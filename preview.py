import logging
from multiprocessing import Process, Pipe
from pathlib import Path
from collections import defaultdict
import re
from enum import Enum
import json

import zmq
import cv2
import numpy as np
from pyglui import ui
import glfw

from plugin import Plugin
from methods import Roi
from zmq_tools import Msg_Receiver
from pupil_detectors import Detector_2D
from vis_eye_video_overlay import get_ellipse_points
from pyglui.cygl.utils import draw_gl_texture
from gl_utils import clear_gl_screen, basic_gl_setup, make_coord_system_norm_based

logger = logging.getLogger("preview")

# For some reason not exported in GLFW
GLFW_FLOATING = 131079


class PreviewFrame:
    """
    The meta data of a frame extracted as a preview.
    """

    class Format(Enum):
        """
        The image format of the exported frame.
        """

        JPEG = "jpg"
        PNG = "png"
        BMP = "bmp"

        def __str__(self) -> str:
            return self.value

        @staticmethod
        def from_path(path: Path) -> "PreviewFrame.Format":
            return PreviewFrame.Format.from_extension(path.suffix[1:])

        @staticmethod
        def from_extension(extension: str) -> "PreviewFrame.Format":
            try:
                return next(
                    format for format in PreviewFrame.Format if str(format) == extension
                )
            except StopIteration:
                raise ValueError("Unknown extension '{}'".format(extension))

    FILE_FORMAT = "eye{}_frame{}_confidence{:05.4f}.{}"

    def __init__(
        self,
        eye_id: int,
        frame_num: int,
        confidence: float,
        frame_format: "PreviewFrame.Format",
    ):
        """
        Creates a new preview.
        :param eye_id: The ID of the eye.
        :param frame_num: The number of frames.
        :param confidence: The confidence of the 2D detection.
        """
        self.eye_id = eye_id
        self.frame_num = frame_num
        self.confidence = confidence
        self.format = frame_format

    def __str__(self):
        return PreviewFrame.FILE_FORMAT.format(
            self.eye_id, self.frame_num, self.confidence, self.format
        )

    def save(self, folder: Path, data: np.ndarray) -> None:
        """
        Write a given image into the file system and save the meta data beside it.
        :param folder: The folder for storing the images.
        :param data: The image itself.
        """
        cv2.imwrite(str(folder / str(self)), data)

    def load(self, folder: Path) -> np.ndarray:
        """
        Load the corresponding image from the file system given the meta data.
        :param path: The folder for storing the images.
        :return: The loaded color image.
        """
        return cv2.imread(str(Path(folder, str(self))))

    @staticmethod
    def load_all(folder: Path) -> "Sequence[Sequence[PreviewFrame]]":
        """
        Load all available image meta data from a folder.
        :param folder: The folder for storing the images.
        :return: A sequence of sequences containing the frames.
        """
        formatting_pattern = re.compile(r"{.*?}")

        # Create a glob-compatible pattern
        file_pattern = formatting_pattern.sub(repl="*", string=PreviewFrame.FILE_FORMAT)

        # Create a extractor for the informarion supporting both (floating) number and strings.
        info_extractor = re.compile(
            formatting_pattern.sub(
                repl="([0-9]+(?:\.[0-9]+)?|[a-z]+)", string=PreviewFrame.FILE_FORMAT
            )
        )

        # Read all available paths and sort them by eye id
        collections = defaultdict(list)
        for file in folder.glob(file_pattern):
            match = info_extractor.fullmatch(file.name)
            if match is None:
                continue

            frame = PreviewFrame(
                eye_id=int(match.group(1)),
                frame_num=int(match.group(2)),
                confidence=float(match.group(3)),
                frame_format=PreviewFrame.Format.from_extension(match.group(4)),
            )
            collections[frame.eye_id].append(frame)

        # Sort collections by the frame number
        for collection in collections.values():
            collection.sort(key=lambda x: x.frame_num)

        return tuple(zip(*tuple(collections.values())))


class PreviewGenerator:
    class ImageStream:
        class FrameWrapper:
            """
            A tiny wrapper for the layout constrained by the detector.
            """

            def __init__(self, image: np.ndarray, color: np.ndarray = None):
                self.width = image.shape[1]
                self.height = image.shape[0]
                self.gray = image
                self.img = color
                self.timestamp = 0

        def __init__(
            self,
            eye_id: int,
            frame_per_frames: int,
            folder: Path,
            frame_size,
            frame_format: PreviewFrame.Format,
            detector_parameters: "Mapping[str, Any]",
        ):
            self.frame_per_frames = frame_per_frames
            self.folder = folder
            self.frame_size = frame_size
            self.eye_id = eye_id
            self.frame_format = frame_format

            self.__counter = 0
            self.__detector = Detector_2D(settings={"pupil_size_min":40, "pupil_size_max":200, "coarse_detection":False})

            # Set custom parameter for the Detector2d, if given
            if len(detector_parameters) > 0:
                # Update settings even if 'set_2d_detector_property' is not available
                settings = self.__detector.get_settings()
                settings.update(detector_parameters)
                self.__detector = Detector_2D(settings=settings)

        def add(self, payload) -> bool:
            self.__counter += 1
            if self.__counter % self.frame_per_frames == 0:
                if payload["format"] not in ("gray", "bgr", "jpeg"):
                    raise NotImplementedError(
                        "The eye frame format '{}' is currently not supported!".format(
                            payload["format"]
                        )
                    )

                shape = [self.frame_size[1], self.frame_size[0]]
                if payload["format"] != "gray":
                    shape.append(3)

                data = np.frombuffer(payload["__raw_data__"][-1], dtype=np.uint8)
                if len(data) == np.prod(shape) or payload["format"] == "jpeg":
                    raw_frame = (
                        cv2.imdecode(data, cv2.IMREAD_COLOR)
                        if payload["format"] == "jpeg"
                        else data.reshape(shape)
                    )

                    # Pupil/OpenCV seems to tamper the underlying data. Better copy it.
                    if payload["format"] == "gray":
                        raw_frame = raw_frame.copy()

                    grayscale_frame = (
                        raw_frame
                        if len(shape) == 2
                        else cv2.cvtColor(raw_frame, cv2.COLOR_BGR2GRAY)
                    )
                    color_frame = (
                        raw_frame
                        if len(shape) == 3
                        else cv2.cvtColor(raw_frame, cv2.COLOR_GRAY2BGR)
                    )

                    # Extract the pupil
                    pupil_2d = self.__detector.detect(
                        frame_=PreviewGenerator.ImageStream.FrameWrapper(
                            grayscale_frame,
                            color_frame
                        ),
                        user_roi=Roi(grayscale_frame.shape),
                        visualize=True,
                    )

                    # Visualize the ellipse
                    # ellipse = pupil_2d["ellipse"]
                    #confidence = pupil_2d["confidence"]
                    #if confidence > 0.0:
                    #    ellipse_points = get_ellipse_points(
                    #        (ellipse["center"], ellipse["axes"], ellipse["angle"]),
                    #        num_pts=50,
                    #    )
                    #    cv2.polylines(
                    #        color_frame,
                    #        [np.asarray(ellipse_points, dtype="i")],
                    #        True,
                    #        (0, 0, 255),
                    #        thickness=2,
                    #    )

                    frame = PreviewFrame(
                        self.eye_id,
                        self.__counter,
                        pupil_2d["confidence"],
                        self.frame_format,
                    )
                    frame.save(self.folder, color_frame)
                    return True
                else:
                    raise RuntimeWarning(
                        "Image size {} does not match expected shape.".format(len(data))
                    )

            return False

        def __bool__(self):
            return self.__counter > 0

    def __init__(
        self,
        url,
        command_pipe,
        exception_pipe,
        frame_per_frames: int,
        folder: Path,
        frame_format: PreviewFrame.Format,
        detector_parameters: "Mapping[str, Any]",
    ):
        if not folder.is_dir():
            raise FileNotFoundError(
                "The given folder '{}' does not exists.".format(folder)
            )

        self.frame_per_frames = frame_per_frames
        self.folder = folder
        self.frame_format = frame_format
        self.detector_parameters = detector_parameters

        self._url = url
        self._command_pipe = command_pipe
        self._status_pipe = exception_pipe

    @staticmethod
    def generate(params: "PreviewGenerator"):
        try:
            # Connect to url and read
            params._status_pipe.send("Connecting to URL '{}'...".format(params._url))
            context = zmq.Context()
            frame_queue = Msg_Receiver(context, params._url, topics=("frame.eye",))
            params._status_pipe.send(
                "Starting generating previews and saving them in '{}'...".format(
                    params.folder
                )
            )

            streams = {}
            while not params._command_pipe.poll():
                if frame_queue.new_data:
                    topic, payload = frame_queue.recv()
                    id = int(str(topic).split(".")[-1])
                    if id not in streams:
                        streams[id] = PreviewGenerator.ImageStream(
                            eye_id=id,
                            frame_per_frames=params.frame_per_frames,
                            folder=params.folder,
                            frame_size=(payload["width"], payload["height"]),
                            frame_format=params.frame_format,
                            detector_parameters=params.detector_parameters,
                        )
                    streams[id].add(payload)

            del frame_queue
        except Exception as e:
            params._status_pipe.send(e)


class PreviewWindow:
    class WindowContextManager:
        """
        A context manager for dealing with different window contexts.
        """

        def __init__(self, next_handle=None):
            self.__next_handle = next_handle
            self.__old_handle = None

        def __enter__(self):
            self.__old_handle = glfw.glfwGetCurrentContext()
            if self.__next_handle is not None:
                glfw.glfwMakeContextCurrent(self.__next_handle)
            return self.__old_handle

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is not None:
                return

            glfw.glfwMakeContextCurrent(self.__old_handle)

    WINDOW_NAME = "Detection Preview"

    def __init__(self, parent: Plugin, path: Path):
        self.path = path
        self.parent = parent
        self.__window = None

    def __bool__(self):
        return self.__window is not None

    def show(self):
        if self.__window is not None:
            raise RuntimeError("Window is already shown.")

        frames = PreviewFrame.load_all(self.path)
        if len(frames) == 0:
            logger.warning(
                "No frames where found. Therefore, the preview is not shown."
            )
            return

        frame_index = 0

        def on_key(window, key, _scancode, action, _mods):
            nonlocal frame_index

            # Respond only to key press
            if action == glfw.GLFW_RELEASE:
                return

            if key == glfw.GLFW_KEY_LEFT and frame_index > 0:
                frame_index -= 1
                PreviewWindow._draw_frame(window, self.path, frames, frame_index, False)
            elif key == glfw.GLFW_KEY_RIGHT and frame_index < len(frames) - 1:
                frame_index += 1
                PreviewWindow._draw_frame(window, self.path, frames, frame_index, False)

        def on_close(_window):
            self.parent.notify_all({"subject": Preview.NOTIFICATION_PREVIEW_CLOSE})

        # TODO: The code assumes for simplicity that both eye images run with the same resolution.
        first_frame = frames[0][0].load(self.path)
        with PreviewWindow.WindowContextManager() as active_window:
            glfw.glfwWindowHint(glfw.GLFW_RESIZABLE, False)
            glfw.glfwWindowHint(glfw.GLFW_ICONIFIED, False)
            glfw.glfwWindowHint(GLFW_FLOATING, True)

            self.__window = glfw.glfwCreateWindow(
                first_frame.shape[1] * len(frames[0]),
                first_frame.shape[0],
                PreviewWindow.WINDOW_NAME,
                monitor=None,
                share=active_window,
            )

            # Reset default
            glfw.glfwWindowHint(glfw.GLFW_RESIZABLE, True)
            glfw.glfwWindowHint(glfw.GLFW_ICONIFIED, True)
            glfw.glfwWindowHint(GLFW_FLOATING, False)

            glfw.glfwSetKeyCallback(self.__window, on_key)
            glfw.glfwSetWindowCloseCallback(self.__window, on_close)
            glfw.glfwMakeContextCurrent(self.__window)
            basic_gl_setup()
            glfw.glfwSwapInterval(0)

        PreviewWindow._draw_frame(self.__window, self.path, frames, 0, True)

    def close(self):
        if self.__window is None:
            raise RuntimeError("Window is already closed.")

        with PreviewWindow.WindowContextManager():
            glfw.glfwDestroyWindow(self.__window)
            self.__window = None

    @staticmethod
    def _draw_frame(window, path, frames, index: int, show_help: bool):
        frames_data = [frame.load(path) for frame in frames[index]]

        for frame, frame_meta in zip(frames_data, frames[index]):
            PreviewWindow._draw_text(
                frame,
                "Preview {}/{} (eye{})".format(
                    index + 1, len(frames), frame_meta.eye_id
                ),
                (15, frame.shape[0] - 30),
            )
            #PreviewWindow._draw_text(
            #    frame,
            #    "Confidence: {}".format(frame_meta.confidence),
            #    (15, frame.shape[0] - 30),
            #)

        frame = frames_data[0] if len(frames_data) == 1 else np.hstack(frames_data)

        # Present usage hints at first load
        if show_help:
            PreviewWindow._draw_text(
                frame,
                "Usage: Use the arrow keys for navigating between frames.",
                (15, 40),
            )

        with PreviewWindow.WindowContextManager(window):
            clear_gl_screen()
            make_coord_system_norm_based()
            draw_gl_texture(frame, interpolation=False)
            glfw.glfwSwapBuffers(window)

    @staticmethod
    def _draw_text(frame, string, position):
        cv2.putText(
            frame,
            string,
            position,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (157, 233, 68),
            2,
            cv2.LINE_AA,
            False,
        )


class Preview(Plugin):
    NOTIFICATION_PREVIEW_SHOW = "preview.show"
    NOTIFICATION_PREVIEW_CLOSE = "preview.close"

    DETECTOR_CONFIG = "user_settings_preview.json"

    icon_chr = "P"
    order = 0.6

    def __init__(
        self,
        g_pool,
        frames_per_frame: int = 1200,
        folder: str = "preview",
        should_show: bool = True,
        frame_format: "Union[str, PreviewFrame.Format.JPEG]" = PreviewFrame.Format.JPEG,
    ):
        super().__init__(g_pool)

        self.__command_sender = None
        self.__worker = None
        self.__status_receiver = None
        self.__generator = None
        self.__window = None
        self.__frame_format: PreviewFrame.Format = None

        self.frames_per_frame = frames_per_frame
        self.folder = folder
        self.should_show = should_show
        self.frame_format = frame_format

    @property
    def frame_format(self):
        return self.__frame_format.name

    @frame_format.setter
    def frame_format(self, value):
        value = (
            value
            if isinstance(value, PreviewFrame.Format)
            else PreviewFrame.Format[value]
        )
        self.__frame_format = value

    @property
    def folder(self):
        return self.__folder

    @folder.setter
    def folder(self, folder):
        if not isinstance(folder, Path):
            folder = Path(folder)
        self.__folder = folder

    def recent_events(self, _events):
        if self.__status_receiver is not None:
            try:
                if self.__status_receiver.poll():
                    status = self.__status_receiver.recv()
                    if isinstance(status, Exception):
                        raise status
                    else:
                        logger.info("{}".format(status))
            except BrokenPipeError:
                self.__status_receiver = None
                self.__command_sender = None

    def on_notify(self, notification):
        subject = notification["subject"]
        if subject == "recording.started" and self.__worker is None:
            path = self.folder
            if not path.is_absolute() or not path.is_dir():
                recording_path = Path(notification["rec_path"])
                path = recording_path / path
                path.mkdir(parents=True)

            self.__generator = self.__create_generator(path)
            self.__worker = Process(
                target=PreviewGenerator.generate, args=(self.__generator,), daemon=True
            )
            self.__worker.start()

        elif (
            subject == "recording.stopped"
            and self.__worker is not None
            and self.__worker.is_alive()
        ):
            self.__command_sender.send("exit")
            self.__worker.join(3)
            assert self.__worker.exitcode is not None, "Joining failed."

            logger.info("Stopping generation of previews.")
            rough_frame_pattern = "*.{}".format(self.__frame_format)
            if len(list(self.__generator.folder.glob(rough_frame_pattern))) == 0:
                logger.warning(
                    "No previews were generated. Was the Frame Publisher activated?!"
                )
            elif self.should_show:
                self.notify_all({"subject": Preview.NOTIFICATION_PREVIEW_SHOW})

            # Reset process properties
            self.__worker = None
            self.__status_receiver = None
            self.__command_sender = None

        elif (
            subject == Preview.NOTIFICATION_PREVIEW_SHOW
            and self.__generator is not None
            and self.__window is None
        ):
            self.__window = PreviewWindow(self, self.__generator.folder)
            self.__window.show()

        elif (
            subject == Preview.NOTIFICATION_PREVIEW_CLOSE
            and self.__window is not None
            and bool(self.__window)
        ):
            self.__window.close()
            self.__window = None

    def get_init_dict(self):
        return {
            "frames_per_frame": self.frames_per_frame,
            "folder": str(self.folder),
            "should_show": self.should_show,
            "frame_format": self.frame_format,
        }

    def clone(self):
        return Preview(**self.get_init_dict())

    def init_ui(self):
        self.add_menu()
        self.menu.label = "Preview of pupil detection"
        self.menu.append(
            ui.Info_Text(
                "This plugin saves a subset of eye images with their 2D detected ellipses for evaluation purposes."
            )
        )
        self.menu.append(
            ui.Slider(
                "frames_per_frame",
                self,
                min=10,
                step=10,
                max=10000,
                label="Frame interval",
            )
        )
        self.menu.append(ui.Text_Input("folder", self, label="Storage"))
        self.menu.append(
            ui.Selector(
                "frame_format",
                self,
                selection=tuple(PreviewFrame.Format.__members__.keys()),
                label="Image format",
            )
        )
        self.menu.append(
            ui.Switch("should_show", self, label="Show preview after recording")
        )

    def deinit_ui(self):
        self.remove_menu()

    def _get_detector_parameters(self) -> "Mapping[str, Any]":
        config_file = Path(self.g_pool.user_dir, Preview.DETECTOR_CONFIG)
        if config_file.is_file():
            logger.info(
                "Loading detector parameters for preview from '%s'.", config_file
            )
            with config_file.open("r", encoding="utf-8-sig") as file:
                try:
                    parameters = json.load(file)
                except json.JSONDecodeError:
                    logger.error(
                        "Unable to load the detector parameters due to invalid JSON. Using default."
                    )
                    parameters = {}
        else:
            parameters = {}

        return parameters

    def __create_generator(self, folder: Path) -> "PreviewGenerator":
        command_receiver, self.__command_sender = Pipe(False)
        self.__status_receiver, status_sender = Pipe(False)
        self.__worker = None
        return PreviewGenerator(
            url=self.g_pool.ipc_sub_url,
            command_pipe=command_receiver,
            exception_pipe=status_sender,
            frame_per_frames=self.frames_per_frame,
            folder=folder,
            frame_format=self.__frame_format,
            detector_parameters=self._get_detector_parameters(),
        )

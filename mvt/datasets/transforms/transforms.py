import inspect
import numpy as np
import random
import math
import cv2

from mvt.utils.mask_util import PolygonMasks
from mvt.utils.bbox_util import bbox_overlaps_np
from mvt.utils.misc_util import is_list_of, is_str
from mvt.utils.geometric_util import (
    imresize,
    imflip,
    impad,
    impad_to_multiple,
    imrescale,
    imcrop,
)
from mvt.utils.photometric_util import (
    imnormalize,
    bgr2hsv,
    hsv2bgr,
    rgb2gray
)
from ..data_wrapper import PIPELINES
from .compose import Compose as PipelineCompose

try:
    from imagecorruptions import corrupt
except ImportError:
    corrupt = None

try:
    import albumentations
    from albumentations import Compose
except ImportError:
    albumentations = None
    Compose = None


@PIPELINES.register_module()
class JointResize(object):
    """Resize images & bbox & mask.
    This transform resizes the input image to some scale. Bboxes and masks are
    then resized with the same scale factor. If the input dict contains the key
    "scale", then the scale in the input dict is used, otherwise the specified
    scale in the init method is used. If the input dict contains the key
    "scale_factor" (if MultiScaleFlipAug does not give img_scale but
    scale_factor), the actual scale will be computed by image shape and
    scale_factor.
    """

    def __init__(
        self,
        img_scale=None,
        multiscale_mode="range",
        ratio_range=None,
        keep_ratio=True,
        backend="cv2",
    ):
        """Initialization for resizing bbox
        `img_scale` can either be a tuple (single-scale) or a list of tuple
        (multi-scale). There are 3 multiscale modes:
        - ``ratio_range is not None``: randomly sample a ratio from the ratio \
        range and multiply it with the image scale.
        - ``ratio_range is None`` and ``multiscale_mode == "range"``: randomly \
        sample a scale from the multiscale range.
        - ``ratio_range is None`` and ``multiscale_mode == "value"``: randomly \
        sample a scale from multiple scales.

        Args:
            img_scale (tuple or list[tuple]): Images scales for resizing.
            multiscale_mode (str): Either "range" or "value".
            ratio_range (tuple[float]): (min_ratio, max_ratio)
            keep_ratio (bool): Whether to keep the aspect ratio when resizing the
                image.
            backend (str): Image resize backend, choices are 'cv2' and 'pillow'.
                These two backends generates slightly different results. Defaults
                to 'cv2'.
        """

        if img_scale is None:
            self.img_scale = None
        else:
            if is_list_of(img_scale, list):
                self.img_scale = img_scale
            else:
                self.img_scale = [img_scale]
            assert is_list_of(self.img_scale, list)

        if ratio_range is not None:
            # mode 1: given a scale and a range of image ratio
            assert len(self.img_scale) == 1
        else:
            # mode 2: given multiple scales or a range of scales
            assert multiscale_mode in ["value", "range"]

        self.backend = backend
        self.multiscale_mode = multiscale_mode
        self.ratio_range = ratio_range
        self.keep_ratio = keep_ratio

    @staticmethod
    def random_select(img_scales):
        """Randomly select an img_scale from given candidates.

        Args:
            img_scales (list[list]): Images scales for selection.

        Returns:
            (list, int): Returns a tuple ``(img_scale, scale_dix)``, \
                where ``img_scale`` is the selected image scale and \
                ``scale_idx`` is the selected index in the given candidates.
        """

        assert is_list_of(img_scales, list)
        scale_idx = np.random.randint(len(img_scales))
        img_scale = img_scales[scale_idx]
        return img_scale, scale_idx

    @staticmethod
    def random_sample(img_scales):
        """Randomly sample an img_scale when ``multiscale_mode=='range'``.

        Args:
            img_scales (list[list]): Images scale range for sampling.
                There must be two tuples in img_scales, which specify the lower
                and uper bound of image scales.

        Returns:
            (list, None): Returns a tuple ``(img_scale, None)``, where \
                ``img_scale`` is sampled scale and None is just a placeholder \
                to be consistent with :func:`random_select`.
        """

        assert is_list_of(img_scales, list) and len(img_scales) == 2
        img_scale_long = [max(s) for s in img_scales]
        img_scale_short = [min(s) for s in img_scales]
        long_edge = np.random.randint(min(img_scale_long), max(img_scale_long) + 1)
        short_edge = np.random.randint(min(img_scale_short), max(img_scale_short) + 1)
        img_scale = (long_edge, short_edge)
        return img_scale, None

    @staticmethod
    def random_sample_ratio(img_scale, ratio_range):
        """Randomly sample an img_scale when ``ratio_range`` is specified.
        A ratio will be randomly sampled from the range specified by
        ``ratio_range``. Then it would be multiplied with ``img_scale`` to
        generate sampled scale.

        Args:
            img_scale (list): Images scale base to multiply with ratio.
            ratio_range (list[float]): The minimum and maximum ratio to scale
                the ``img_scale``.

        Returns:
            (tuple, None): Returns a tuple ``(scale, None)``, where \
                ``scale`` is sampled ratio multiplied with ``img_scale`` and \
                None is just a placeholder to be consistent with \
                :func:`random_select`.
        """

        assert isinstance(img_scale, list) and len(img_scale) == 2
        min_ratio, max_ratio = ratio_range
        assert min_ratio <= max_ratio
        ratio = np.random.random_sample() * (max_ratio - min_ratio) + min_ratio
        scale = int(img_scale[0] * ratio), int(img_scale[1] * ratio)
        return scale, None

    def _random_scale(self, results):
        """Randomly sample an img_scale according to ``ratio_range`` and
        ``multiscale_mode``.
        If ``ratio_range`` is specified, a ratio will be sampled and be
        multiplied with ``img_scale``.
        If multiple scales are specified by ``img_scale``, a scale will be
        sampled according to ``multiscale_mode``.
        Otherwise, single scale will be used.

        Args:
            results (dict): Result dict from :obj:`dataset`.

        Returns:
            dict: Two new keys 'scale` and 'scale_idx` are added into \
                ``results``, which would be used by subsequent pipelines.
        """

        if self.ratio_range is not None:
            scale, scale_idx = self.random_sample_ratio(
                self.img_scale[0], self.ratio_range
            )
        elif len(self.img_scale) == 1:
            scale, scale_idx = self.img_scale[0], 0
        elif self.multiscale_mode == "range":
            scale, scale_idx = self.random_sample(self.img_scale)
        elif self.multiscale_mode == "value":
            scale, scale_idx = self.random_select(self.img_scale)
        else:
            raise NotImplementedError

        results["scale"] = scale
        results["scale_idx"] = scale_idx

    def _resize_img(self, results):
        """Resize images with ``results['scale']``."""

        for key in results.get("img_fields", ["img"]):
            if self.keep_ratio:
                img, scale_factor = imrescale(
                    results[key],
                    results["scale"],
                    return_scale=True,
                    backend=self.backend,
                )
                # the w_scale and h_scale has minor difference
                # a real fix should be done in the imrescale in the future
                new_h, new_w = img.shape[:2]
                h, w = results[key].shape[:2]
                w_scale = new_w / w
                h_scale = new_h / h
            else:
                img, w_scale, h_scale = imresize(
                    results[key],
                    results["scale"],
                    return_scale=True,
                    backend=self.backend,
                )
            results[key] = img

            scale_factor = np.array(
                [w_scale, h_scale, w_scale, h_scale], dtype=np.float32
            )
            results["img_shape"] = img.shape

            # in case that there is no padding
            results["pad_shape"] = img.shape
            results["scale_factor"] = scale_factor
            results["keep_ratio"] = self.keep_ratio

    def _resize_bboxes(self, results):
        """Resize bounding boxes with ``results['scale_factor']``."""

        img_shape = results["img_shape"]
        for key in results.get("bbox_fields", []):
            bboxes = results[key] * results["scale_factor"]
            bboxes[:, 0::2] = np.clip(bboxes[:, 0::2], 0, img_shape[1])
            bboxes[:, 1::2] = np.clip(bboxes[:, 1::2], 0, img_shape[0])
            results[key] = bboxes

    def _resize_masks(self, results):
        """Resize masks with ``results['scale']``"""

        for key in results.get("mask_fields", []):
            if results[key] is None:
                continue
            if self.keep_ratio:
                results[key] = results[key].rescale(results["scale"])
            else:
                results[key] = results[key].resize(results["img_shape"][:2])

    def _resize_seg(self, results):
        """Resize semantic segmentation map with ``results['scale']``."""

        for key in results.get("seg_fields", []):
            if self.keep_ratio:
                gt_seg = imrescale(
                    results[key],
                    results["scale"],
                    interpolation="nearest",
                    backend=self.backend,
                )
            else:
                gt_seg = imresize(
                    results[key],
                    results["scale"],
                    interpolation="nearest",
                    backend=self.backend,
                )
            results["gt_semantic_seg"] = gt_seg

    def __call__(self, results):
        """Call function to resize images, bounding boxes, masks, semantic
        segmentation map.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Resized results, 'img_shape', 'pad_shape', 'scale_factor', \
                'keep_ratio' keys are added into result dict.
        """

        if "scale" not in results:
            if "scale_factor" in results:
                img_shape = results["img"].shape[:2]
                scale_factor = results["scale_factor"]
                assert isinstance(scale_factor, float)
                results["scale"] = tuple(
                    [int(x * scale_factor) for x in img_shape][::-1]
                )
            else:
                self._random_scale(results)
        else:
            assert (
                "scale_factor" not in results
            ), "scale and scale_factor cannot be both set."

        self._resize_img(results)
        self._resize_bboxes(results)
        self._resize_masks(results)
        self._resize_seg(results)
        return results

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(img_scale={self.img_scale}, "
        repr_str += f"multiscale_mode={self.multiscale_mode}, "
        repr_str += f"ratio_range={self.ratio_range}, "
        repr_str += f"keep_ratio={self.keep_ratio})"
        return repr_str


@PIPELINES.register_module()
class LetterResize:
    """from https://github.com/ultralytics/yolov5"""

    def __init__(
        self,
        img_scale=None,
        color=(114, 114, 114),
        auto=True,
        scaleFill=False,
        scaleup=True,
        backend="cv2",
    ):
        self.image_size_hw = img_scale
        self.color = color
        self.auto = auto
        self.scaleFill = scaleFill
        self.scaleup = scaleup
        self.backend = backend

    def __call__(self, results):

        for key in results.get("img_fields", ["img"]):
            img = results[key]

            shape = img.shape[:2]  # current shape [height, width]
            if isinstance(self.image_size_hw, int):
                self.image_size_hw = (self.image_size_hw, self.image_size_hw)

            # Scale ratio (new / old)
            r = min(self.image_size_hw[0] / shape[0], self.image_size_hw[1] / shape[1])
            if (
                not self.scaleup
            ):  # only scale down, do not scale up (for better test mAP)
                r = min(r, 1.0)
            ratio = r, r
            # find most proper size
            new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
            # pad for fixed size
            dw, dh = (
                self.image_size_hw[1] - new_unpad[0],
                self.image_size_hw[0] - new_unpad[1],
            )  # wh padding
            if self.auto:  # minimum rectangle
                dw, dh = np.mod(dw, 64), np.mod(dh, 64)  # wh padding
            elif self.scaleFill:  # stretch
                dw, dh = 0.0, 0.0
                # scale to fixed size
                new_unpad = (self.image_size_hw[1], self.image_size_hw[0])
                ratio = (
                    self.image_size_hw[1] / shape[1],
                    self.image_size_hw[0] / shape[0],
                )  # width, height ratios

            # padding for left and right
            dw /= 2  # divide padding into 2 sides
            dh /= 2

            # no padding
            if shape[::-1] != new_unpad:  # resize
                img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
            results["img_shape"] = img.shape
            scale_factor = np.array(
                [ratio[0], ratio[1], ratio[0], ratio[1]], dtype=np.float32
            )
            results["scale_factor"] = scale_factor

            # padding
            top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
            left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
            img = cv2.copyMakeBorder(
                img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=self.color
            )  # add border

            results[key] = img

            results["pad_shape"] = img.shape
            results["pad_param"] = np.array(
                [top, bottom, left, right], dtype=np.float32
            )
        return results


@PIPELINES.register_module()
class ImgResize:
    """Resize images for classification."""

    def __init__(self, size, interpolation="bilinear", backend="cv2"):
        """Initialization of resize operator for classification.

        Args:
            size (int | tuple): Images scales for resizing (h, w).
                When size is int, the default behavior is to resize an image
                to (size, size). When size is tuple and the second value is -1,
                the short edge of an image is resized to its first value.
                For example, when size is 224, the image is resized to 224x224.
                When size is (224, -1), the short side is resized to 224 and the
                other side is computed based on the short side, maintaining the
                aspect ratio.
            interpolation (str): Interpolation method, accepted values are
                "nearest", "bilinear", "bicubic", "area", "lanczos".
                More details can be found in `geometric`.
            backend (str): The image resize backend type, accpeted values are
                `cv2` and `pillow`. Default: `cv2`.
        """
        assert isinstance(size, int) or (isinstance(size, tuple) and len(size) == 2)
        self.resize_w_short_side = False
        if isinstance(size, int):
            assert size > 0
            size = (size, size)
        else:
            assert size[0] > 0 and (size[1] > 0 or size[1] == -1)
            if size[1] == -1:
                self.resize_w_short_side = True
        assert interpolation in ("nearest", "bilinear", "bicubic", "area", "lanczos")
        if backend not in ["cv2", "pillow"]:
            raise ValueError(
                f"backend: {backend} is not supported for resize."
                'Supported backends are "cv2", "pillow"'
            )

        self.size = size
        self.interpolation = interpolation
        self.backend = backend

    def _resize_img(self, results):

        for key in results.get("img_fields", ["img"]):
            img = results[key]
            ignore_resize = False
            if self.resize_w_short_side:
                h, w = img.shape[:2]
                short_side = self.size[0]
                if (w <= h and w == short_side) or (h <= w and h == short_side):
                    ignore_resize = True
                else:
                    if w < h:
                        width = short_side
                        height = int(short_side * h / w)
                    else:
                        height = short_side
                        width = int(short_side * w / h)
            else:
                height, width = self.size
            if not ignore_resize:
                img = imresize(
                    img,
                    size=(width, height),
                    interpolation=self.interpolation,
                    return_scale=False,
                    backend=self.backend,
                )
                results[key] = img
                results["img_shape"] = img.shape

    def __call__(self, results):

        self._resize_img(results)
        return results

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(size={self.size}, "
        repr_str += f"interpolation={self.interpolation})"
        return repr_str


@PIPELINES.register_module()
class RandomGrayscale:
    """Randomly convert image to grayscale with a probability of gray_prob."""

    def __init__(self, gray_prob=0.1):
        """Initialization for random grayscale

        Args:
            gray_prob (float): Probability that image should be converted to
                grayscale. Default: 0.1.

        Returns:
            ndarray: Grayscale version of the input image with probability
                gray_prob and unchanged with probability (1-gray_prob).
                - If input image is 1 channel: grayscale version is 1 channel.
                - If input image is 3 channel: grayscale version is 3 channel
                    with r == g == b.
        """

        self.gray_prob = gray_prob

    def __call__(self, results):
        """
        Args:
            img (ndarray): Image to be converted to grayscale.

        Returns:
            ndarray: Randomly grayscaled image.
        """

        for key in results.get("img_fields", ["img"]):
            img = results[key]
            num_output_channels = img.shape[2]
            if random.random() < self.gray_prob:
                if num_output_channels > 1:
                    img = rgb2gray(img)[:, :, None]
                    results[key] = np.dstack([img for _ in range(num_output_channels)])
                    return results
            results[key] = img
        return results

    def __repr__(self):

        return self.__class__.__name__ + f"(gray_prob={self.gray_prob})"


@PIPELINES.register_module()
class JointRandomFlip:
    """Flip the image & bbox & mask.
    If the input dict contains the key "flip", then the flag will be used,
    otherwise it will be randomly decided by a ratio specified in the init
    method.
    """

    def __init__(self, flip_ratio=None, direction="horizontal"):
        """Initialization for bbox random flip
        When random flip is enabled, ``flip_ratio``/``direction`` can either be a
        float/string or tuple of float/string. There are 3 flip modes:
        - ``flip_ratio`` is float, ``direction`` is string: the image will be
            ``direction``ly flipped with probability of ``flip_ratio`` .
            E.g., ``flip_ratio=0.5``, ``direction='horizontal'``,
            then image will be horizontally flipped with probability of 0.5.
        - ``flip_ratio`` is float, ``direction`` is list of string: the image wil
            be ``direction[i]``ly flipped with probability of
            ``flip_ratio/len(direction)``.
            E.g., ``flip_ratio=0.5``, ``direction=['horizontal', 'vertical']``,
            then image will be horizontally flipped with probability of 0.25,
            vertically with probability of 0.25.
        - ``flip_ratio`` is list of float, ``direction`` is list of string:
            given ``len(flip_ratio) == len(direction)``, the image wil
            be ``direction[i]``ly flipped with probability of ``flip_ratio[i]``.
            E.g., ``flip_ratio=[0.3, 0.5]``, ``direction=['horizontal',
            'vertical']``, then image will be horizontally flipped with probability
            of 0.3, vertically with probability of 0.5

        Args:
            flip_ratio (float | list[float], optional): The flipping probability.
                Default: None.
            direction(str | list[str], optional): The flipping direction. Options
                are 'horizontal', 'vertical', 'diagonal'. Default: 'horizontal'.
                If input is a list, the length must equal ``flip_ratio``. Each
                element in ``flip_ratio`` indicates the flip probability of
                corresponding direction.
        """

        if isinstance(flip_ratio, list):
            assert is_list_of(flip_ratio, float)
            assert 0 <= sum(flip_ratio) <= 1
        elif isinstance(flip_ratio, float):
            assert 0 <= flip_ratio <= 1
        elif flip_ratio is None:
            pass
        else:
            raise ValueError("flip_ratios must be None, float, " "or list of float")
        self.flip_ratio = flip_ratio

        valid_directions = ["horizontal", "vertical", "diagonal"]
        if isinstance(direction, str):
            assert direction in valid_directions
        elif isinstance(direction, list):
            assert is_list_of(direction, str)
            assert set(direction).issubset(set(valid_directions))
        else:
            raise ValueError("direction must be either str or list of str")
        self.direction = direction

        if isinstance(flip_ratio, list):
            assert len(self.flip_ratio) == len(self.direction)

    def bbox_flip(self, bboxes, img_shape, direction):
        """Flip bboxes horizontally.

        Args:
            bboxes (numpy.ndarray): Bounding boxes, shape (..., 4*k)
            img_shape (tuple[int]): Image shape (height, width)
            direction (str): Flip direction. Options are 'horizontal',
                'vertical'.

        Returns:
            numpy.ndarray: Flipped bounding boxes.
        """

        assert bboxes.shape[-1] % 4 == 0
        flipped = bboxes.copy()
        if direction == "horizontal":
            w = img_shape[1]
            flipped[..., 0::4] = w - bboxes[..., 2::4]
            flipped[..., 2::4] = w - bboxes[..., 0::4]
        elif direction == "vertical":
            h = img_shape[0]
            flipped[..., 1::4] = h - bboxes[..., 3::4]
            flipped[..., 3::4] = h - bboxes[..., 1::4]
        elif direction == "diagonal":
            w = img_shape[1]
            h = img_shape[0]
            flipped[..., 0::4] = w - bboxes[..., 2::4]
            flipped[..., 1::4] = h - bboxes[..., 3::4]
            flipped[..., 2::4] = w - bboxes[..., 0::4]
            flipped[..., 3::4] = h - bboxes[..., 1::4]
        else:
            raise ValueError(f"Invalid flipping direction '{direction}'")
        return flipped

    def __call__(self, results):
        """Call function to flip bounding boxes, masks, semantic segmentation
        maps.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Flipped results, 'flip', 'flip_direction' keys are added \
                into result dict.
        """

        if "flip" not in results:
            if isinstance(self.direction, list):
                # None means non-flip
                direction_list = self.direction + [None]
            else:
                # None means non-flip
                direction_list = [self.direction, None]

            if isinstance(self.flip_ratio, list):
                non_flip_ratio = 1 - sum(self.flip_ratio)
                flip_ratio_list = self.flip_ratio + [non_flip_ratio]
            else:
                non_flip_ratio = 1 - self.flip_ratio
                # exclude non-flip
                single_ratio = self.flip_ratio / (len(direction_list) - 1)
                flip_ratio_list = [single_ratio] * (len(direction_list) - 1) + [
                    non_flip_ratio
                ]

            cur_dir = np.random.choice(direction_list, p=flip_ratio_list)

            results["flip"] = cur_dir is not None
        if "flip_direction" not in results:
            results["flip_direction"] = cur_dir
        if results["flip"]:
            # flip image
            for key in results.get("img_fields", ["img"]):
                results[key] = imflip(results[key], direction=results["flip_direction"])
            # flip bboxes
            for key in results.get("bbox_fields", []):
                results[key] = self.bbox_flip(
                    results[key], results["img_shape"], results["flip_direction"]
                )
            # flip masks
            for key in results.get("mask_fields", []):
                results[key] = results[key].flip(results["flip_direction"])

            # flip segs
            for key in results.get("seg_fields", []):
                results[key] = imflip(results[key], direction=results["flip_direction"])
        return results

    def __repr__(self):

        return self.__class__.__name__ + f"(flip_ratio={self.flip_ratio})"


@PIPELINES.register_module()
class ImgRandomFlip:
    """Flip the image randomly.
    Flip the image randomly based on flip probaility and flip direction.
    """

    def __init__(self, flip_prob=0.5, direction="horizontal"):
        """Initialization of random flip for classification

        Args:
            flip_prob (float): probability of the image being flipped. Default: 0.5
            direction (str, optional): The flipping direction. Options are
                'horizontal' and 'vertical'. Default: 'horizontal'.
        """

        assert 0 <= flip_prob <= 1
        assert direction in ["horizontal", "vertical"]
        self.flip_prob = flip_prob
        self.direction = direction

    def __call__(self, results):
        """Call function to flip image.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Flipped results, 'flip', 'flip_direction' keys are added into
                result dict.
        """

        flip = True if np.random.rand() < self.flip_prob else False
        results["flip"] = flip
        results["flip_direction"] = self.direction
        if results["flip"]:
            # flip image
            for key in results.get("img_fields", ["img"]):
                results[key] = imflip(results[key], direction=results["flip_direction"])
        return results

    def __repr__(self):

        return self.__class__.__name__ + f"(flip_prob={self.flip_prob})"


@PIPELINES.register_module()
class Pad:
    """Pad the image & mask.

    There are two padding modes: (1) pad to a fixed size and (2) pad to the
    minimum size that is divisible by some number.
    """

    def __init__(self, size=None, size_divisor=None, pad_val=0, seg_pad_val=255):
        """Initialization for padding images

        Args:
            size (tuple, optional): Fixed padding size.
            size_divisor (int, optional): The divisor of padded size.
            pad_val (float, optional): Padding value, 0 by default.
        """

        self.size = size
        self.size_divisor = size_divisor
        self.pad_val = pad_val
        self.seg_pad_val = seg_pad_val
        # only one of size and size_divisor should be valid
        assert size is not None or size_divisor is not None
        assert size is None or size_divisor is None

    def _pad_img(self, results):
        """Pad images according to ``self.size``."""

        for key in results.get("img_fields", ["img"]):
            if self.size is not None:
                padded_img = impad(results[key], shape=self.size, pad_val=self.pad_val)
            elif self.size_divisor is not None:
                padded_img = impad_to_multiple(
                    results[key], self.size_divisor, pad_val=self.pad_val
                )
            results[key] = padded_img
        results["pad_shape"] = padded_img.shape
        results["pad_fixed_size"] = self.size
        results["pad_size_divisor"] = self.size_divisor

    def _pad_masks(self, results):
        """Pad masks according to ``results['pad_shape']``."""

        pad_shape = results["pad_shape"][:2]
        for key in results.get("mask_fields", []):
            results[key] = results[key].pad(pad_shape, pad_val=self.pad_val)

    def _pad_seg(self, results):
        """Pad semantic segmentation map according to
        ``results['pad_shape']``."""

        for key in results.get("seg_fields", []):
            results[key] = impad(
                results[key], shape=results["pad_shape"][:2], pad_val=self.seg_pad_val
            )

    def __call__(self, results):
        """Call function to pad images, masks, semantic segmentation maps.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Updated result dict.
        """
        self._pad_img(results)
        self._pad_masks(results)
        self._pad_seg(results)
        return results

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(size={self.size}, "
        repr_str += f"size_divisor={self.size_divisor}, "
        repr_str += f"pad_val={self.pad_val})"
        repr_str += f"seg_pad_val={self.seg_pad_val})"
        return repr_str


@PIPELINES.register_module()
class Normalize:
    """Normalize the image."""

    def __init__(self, mean, std, to_rgb=True):
        """Initialization for normalization.

        Args:
            mean (sequence): Mean values of 3 channels.
            std (sequence): Std values of 3 channels.
            to_rgb (bool): Whether to convert the image from BGR to RGB,
                default is true.
        """

        self.mean = np.array(mean, dtype=np.float32)
        self.std = np.array(std, dtype=np.float32)
        self.to_rgb = to_rgb

    def __call__(self, results):
        """Call function to normalize images.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Normalized results, 'img_norm_cfg' key is added into
                result dict.
        """
        for key in results.get("img_fields", ["img"]):
            results[key] = imnormalize(results[key], self.mean, self.std, self.to_rgb)
        results["img_norm_cfg"] = dict(mean=self.mean, std=self.std, to_rgb=self.to_rgb)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f"(mean={self.mean}, std={self.std}, to_rgb={self.to_rgb})"
        return repr_str


@PIPELINES.register_module()
class JointRandomCrop:
    """Random crop the image & bboxes & masks."""

    def __init__(self, crop_size, allow_negative_crop=False):
        """Initialization for bbox random crop.

        Args:
            crop_size (tuple): Expected size after cropping, (h, w).
            allow_negative_crop (bool): Whether to allow a crop that does not
                contain any bbox area. Default to False.

        Note:
            - If the image is smaller than the crop size, return the original image
            - The keys for bboxes, labels and masks must be aligned. That is,
            `gt_bboxes` corresponds to `gt_labels` and `gt_masks`, and
            `gt_bboxes_ignore` corresponds to `gt_labels_ignore` and
            `gt_masks_ignore`.
            - If the crop does not contain any gt-bbox region and
            `allow_negative_crop` is set to False, skip this image.
        """

        assert crop_size[0] > 0 and crop_size[1] > 0
        self.crop_size = crop_size
        self.allow_negative_crop = allow_negative_crop
        # The key correspondence from bboxes to labels and masks.
        self.bbox2label = {
            "gt_bboxes": "gt_labels",
            "gt_bboxes_ignore": "gt_labels_ignore",
        }
        self.bbox2mask = {
            "gt_bboxes": "gt_masks",
            "gt_bboxes_ignore": "gt_masks_ignore",
        }

    def __call__(self, results):
        """Call function to randomly crop images, bounding boxes, masks,
        semantic segmentation maps.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Randomly cropped results, 'img_shape' key in result dict is
                updated according to crop size.
        """

        for key in results.get("img_fields", ["img"]):
            img = results[key]
            margin_h = max(img.shape[0] - self.crop_size[0], 0)
            margin_w = max(img.shape[1] - self.crop_size[1], 0)
            offset_h = np.random.randint(0, margin_h + 1)
            offset_w = np.random.randint(0, margin_w + 1)
            crop_y1, crop_y2 = offset_h, offset_h + self.crop_size[0]
            crop_x1, crop_x2 = offset_w, offset_w + self.crop_size[1]

            # crop the image
            img = img[crop_y1:crop_y2, crop_x1:crop_x2, ...]
            img_shape = img.shape
            results[key] = img
        results["img_shape"] = img_shape

        # crop bboxes accordingly and clip to the image boundary
        for key in results.get("bbox_fields", []):
            # e.g. gt_bboxes and gt_bboxes_ignore
            bbox_offset = np.array(
                [offset_w, offset_h, offset_w, offset_h], dtype=np.float32
            )
            bboxes = results[key] - bbox_offset
            bboxes[:, 0::2] = np.clip(bboxes[:, 0::2], 0, img_shape[1])
            bboxes[:, 1::2] = np.clip(bboxes[:, 1::2], 0, img_shape[0])
            valid_inds = (bboxes[:, 2] > bboxes[:, 0]) & (bboxes[:, 3] > bboxes[:, 1])
            # If the crop does not contain any gt-bbox area and
            # self.allow_negative_crop is False, skip this image.
            if (
                key == "gt_bboxes"
                and not valid_inds.any()
                and not self.allow_negative_crop
            ):
                return None
            results[key] = bboxes[valid_inds, :]
            # label fields. e.g. gt_labels and gt_labels_ignore
            label_key = self.bbox2label.get(key)
            if label_key in results:
                results[label_key] = results[label_key][valid_inds]

            # mask fields, e.g. gt_masks and gt_masks_ignore
            mask_key = self.bbox2mask.get(key)
            if mask_key in results:
                results[mask_key] = results[mask_key][valid_inds.nonzero()[0]].crop(
                    np.asarray([crop_x1, crop_y1, crop_x2, crop_y2])
                )

        # crop semantic seg
        for key in results.get("seg_fields", []):
            results[key] = results[key][crop_y1:crop_y2, crop_x1:crop_x2]

        return results

    def __repr__(self):

        return self.__class__.__name__ + f"(crop_size={self.crop_size})"


@PIPELINES.register_module()
class SegRandomCrop:
    """Random crop the image & seg.

    Args:
        crop_size (tuple): Expected size after cropping, (h, w).
        cat_max_ratio (float): The maximum ratio that single category could
            occupy.
    """

    def __init__(self, crop_size, cat_max_ratio=1.0, ignore_index=255):
        assert crop_size[0] > 0 and crop_size[1] > 0
        self.crop_size = crop_size
        self.cat_max_ratio = cat_max_ratio
        self.ignore_index = ignore_index

    def get_crop_bbox(self, img):
        """Randomly get a crop bounding box."""
        margin_h = max(img.shape[0] - self.crop_size[0], 0)
        margin_w = max(img.shape[1] - self.crop_size[1], 0)
        offset_h = np.random.randint(0, margin_h + 1)
        offset_w = np.random.randint(0, margin_w + 1)
        crop_y1, crop_y2 = offset_h, offset_h + self.crop_size[0]
        crop_x1, crop_x2 = offset_w, offset_w + self.crop_size[1]

        return crop_y1, crop_y2, crop_x1, crop_x2

    def crop(self, img, crop_bbox):
        """Crop from ``img``"""
        crop_y1, crop_y2, crop_x1, crop_x2 = crop_bbox
        img = img[crop_y1:crop_y2, crop_x1:crop_x2, ...]
        return img

    def __call__(self, results):
        """Call function to randomly crop images, semantic segmentation maps.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Randomly cropped results, 'img_shape' key in result dict is
                updated according to crop size.
        """

        img = results["img"]
        crop_bbox = self.get_crop_bbox(img)
        if self.cat_max_ratio < 1.0:
            # Repeat 10 times
            for _ in range(10):
                seg_temp = self.crop(results["gt_semantic_seg"], crop_bbox)
                labels, cnt = np.unique(seg_temp, return_counts=True)
                cnt = cnt[labels != self.ignore_index]
                if len(cnt) > 1 and np.max(cnt) / np.sum(cnt) < self.cat_max_ratio:
                    break
                crop_bbox = self.get_crop_bbox(img)

        # crop the image
        img = self.crop(img, crop_bbox)
        img_shape = img.shape
        results["img"] = img
        results["img_shape"] = img_shape

        # crop semantic seg
        for key in results.get("seg_fields", []):
            results[key] = self.crop(results[key], crop_bbox)

        return results

    def __repr__(self):
        return self.__class__.__name__ + f"(crop_size={self.crop_size})"


@PIPELINES.register_module()
class ImgRandomCrop:
    """Crop the given Image at a random location."""

    def __init__(
        self,
        size,
        padding=None,
        pad_if_needed=False,
        pad_val=0,
        padding_mode="constant",
    ):
        """Initialization of random crop for classification

        Args:
            size (sequence or int): Desired output size of the crop. If size is an
                int instead of sequence like (h, w), a square crop (size, size) is
                made.
            padding (int or sequence, optional): Optional padding on each border
                of the image. If a sequence of length 4 is provided, it is used to
                pad left, top, right, bottom borders respectively.  If a sequence
                of length 2 is provided, it is used to pad left/right, top/bottom
                borders, respectively. Default: None, which means no padding.
            pad_if_needed (boolean): It will pad the image if smaller than the
                desired size to avoid raising an exception. Since cropping is done
                after padding, the padding seems to be done at a random offset.
                Default: False.
            pad_val (Number | Sequence[Number]): Pixel pad_val value for constant
                fill. If a tuple of length 3, it is used to pad_val R, G, B
                channels respectively. Default: 0.
            padding_mode (str): Type of padding. Should be: constant, edge,
                reflect or symmetric. Default: constant.
                -constant: Pads with a constant value, this value is specified
                    with pad_val.
                -edge: pads with the last value at the edge of the image.
                -reflect: Pads with reflection of image without repeating the
                    last value on the edge. For example, padding [1, 2, 3, 4]
                    with 2 elements on both sides in reflect mode will result
                    in [3, 2, 1, 2, 3, 4, 3, 2].
                -symmetric: Pads with reflection of image repeating the last
                    value on the edge. For example, padding [1, 2, 3, 4] with
                    2 elements on both sides in symmetric mode will result in
                    [2, 1, 1, 2, 3, 4, 4, 3].
        """

        if isinstance(size, (tuple, list)):
            self.size = size
        else:
            self.size = (size, size)
        # check padding mode
        assert padding_mode in ["constant", "edge", "reflect", "symmetric"]
        self.padding = padding
        self.pad_if_needed = pad_if_needed
        self.pad_val = pad_val
        self.padding_mode = padding_mode

    @staticmethod
    def get_params(img, output_size):
        """Get parameters for ``crop`` for a random crop.

        Args:
            img (ndarray): Image to be cropped.
            output_size (tuple): Expected output size of the crop.

        Returns:
            tuple: Params (xmin, ymin, target_height, target_width) to be
                passed to ``crop`` for random crop.
        """

        height = img.shape[0]
        width = img.shape[1]
        target_height, target_width = output_size
        if width == target_width and height == target_height:
            return 0, 0, height, width

        xmin = np.random.randint(0, height - target_height)
        ymin = np.random.randint(0, width - target_width)
        return xmin, ymin, target_height, target_width

    def __call__(self, results):
        """Call for running

        Args:
            img (ndarray): Image to be cropped.
        """

        for key in results.get("img_fields", ["img"]):
            img = results[key]
            if self.padding is not None:
                img = impad(img, padding=self.padding, pad_val=self.pad_val)

            # pad the height if needed
            if self.pad_if_needed and img.shape[0] < self.size[0]:
                img = impad(
                    img,
                    padding=(
                        0,
                        self.size[0] - img.shape[0],
                        0,
                        self.size[0] - img.shape[0],
                    ),
                    pad_val=self.pad_val,
                    padding_mode=self.padding_mode,
                )

            # pad the width if needed
            if self.pad_if_needed and img.shape[1] < self.size[1]:
                img = impad(
                    img,
                    padding=(
                        self.size[1] - img.shape[1],
                        0,
                        self.size[1] - img.shape[1],
                        0,
                    ),
                    pad_val=self.pad_val,
                    padding_mode=self.padding_mode,
                )

            xmin, ymin, height, width = self.get_params(img, self.size)
            results[key] = imcrop(
                img, np.array([ymin, xmin, ymin + width - 1, xmin + height - 1])
            )
        return results

    def __repr__(self):

        return self.__class__.__name__ + f"(size={self.size}, padding={self.padding})"


@PIPELINES.register_module()
class ImgRandomResizedCrop:
    """Crop the given image to random size and aspect ratio.
    A crop of random size (default: of 0.08 to 1.0) of the original size and a
    random aspect ratio (default: of 3/4 to 4/3) of the original aspect ratio
    is made. This crop is finally resized to given size.
    """

    def __init__(
        self,
        size,
        scale=(0.08, 1.0),
        ratio=(3.0 / 4.0, 4.0 / 3.0),
        interpolation="bilinear",
        backend="cv2",
    ):
        """Initialization of random resized crop for classification

        Args:
            size (sequence or int): Desired output size of the crop. If size is an
                int instead of sequence like (h, w), a square crop (size, size) is
                made.
            scale (tuple): Range of the random size of the cropped image compared
                to the original image. Default: (0.08, 1.0).
            ratio (tuple): Range of the random aspect ratio of the cropped image
                compared to the original image. Default: (3. / 4., 4. / 3.).
            interpolation (str): Interpolation method, accepted values are
                'nearest', 'bilinear', 'bicubic', 'area', 'lanczos'. Default:
                'bilinear'.
            backend (str): The image resize backend type, accpeted values are
                `cv2` and `pillow`. Default: `cv2`.
        """

        if isinstance(size, (tuple, list)):
            self.size = size
        else:
            self.size = (size, size)
        if (scale[0] > scale[1]) or (ratio[0] > ratio[1]):
            raise ValueError(
                "range should be of kind (min, max). " f"But received {scale}"
            )
        if backend not in ["cv2", "pillow"]:
            raise ValueError(
                f"backend: {backend} is not supported for resize."
                'Supported backends are "cv2", "pillow"'
            )

        self.interpolation = interpolation
        self.scale = scale
        self.ratio = ratio
        self.backend = backend

    @staticmethod
    def get_params(img, scale, ratio):
        """Get parameters for ``crop`` for a random sized crop.

        Args:
            img (ndarray): Image to be cropped.
            scale (tuple): Range of the random size of the cropped image
                compared to the original image size.
            ratio (tuple): Range of the random aspect ratio of the cropped
                image compared to the original image area.

        Returns:
            tuple: Params (xmin, ymin, target_height, target_width) to be
                passed to ``crop`` for a random sized crop.
        """

        height = img.shape[0]
        width = img.shape[1]
        area = height * width

        for _ in range(10):
            target_area = np.random.uniform(*scale) * area
            log_ratio = (math.log(ratio[0]), math.log(ratio[1]))
            aspect_ratio = math.exp(np.random.uniform(*log_ratio))

            target_width = int(round(math.sqrt(target_area * aspect_ratio)))
            target_height = int(round(math.sqrt(target_area / aspect_ratio)))

            if 0 < target_width <= width and 0 < target_height <= height:
                xmin = np.random.randint(0, height - target_height)
                ymin = np.random.randint(0, width - target_width)
                return xmin, ymin, target_height, target_width

        # Fallback to central crop
        in_ratio = float(width) / float(height)
        if in_ratio < min(ratio):
            target_width = width
            target_height = int(round(target_width / min(ratio)))
        elif in_ratio > max(ratio):
            target_height = height
            target_width = int(round(target_height * max(ratio)))
        else:  # whole image
            target_width = width
            target_height = height
        xmin = (height - target_height) // 2
        ymin = (width - target_width) // 2
        return xmin, ymin, target_height, target_width

    def __call__(self, results):
        """Call for running
        Args:
            img (ndarray): Image to be cropped and resized.

        Returns:
            ndarray: Randomly cropped and resized image.
        """

        for key in results.get("img_fields", ["img"]):
            img = results[key]
            xmin, ymin, target_height, target_width = self.get_params(
                img, self.scale, self.ratio
            )
            img = imcrop(
                img,
                np.array(
                    [ymin, xmin, ymin + target_width - 1, xmin + target_height - 1]
                ),
            )
            results[key] = imresize(
                img,
                tuple(self.size[::-1]),
                interpolation=self.interpolation,
                backend=self.backend,
            )
        return results

    def __repr__(self):

        format_string = self.__class__.__name__ + f"(size={self.size}"
        format_string += f", scale={tuple(round(s, 4) for s in self.scale)}"
        format_string += f", ratio={tuple(round(r, 4) for r in self.ratio)}"
        format_string += f", interpolation={self.interpolation})"
        return format_string


@PIPELINES.register_module()
class ImgCenterCrop:
    """Center crop the image."""

    def __init__(self, crop_size):
        """Initialization of center crop for classification.

        Args:
            crop_size (int | tuple): Expected size after cropping, (h, w).

        Notes:
            If the image is smaller than the crop size, return the original image
        """

        assert isinstance(crop_size, int) or (
            isinstance(crop_size, tuple) and len(crop_size) == 2
        )
        if isinstance(crop_size, int):
            crop_size = (crop_size, crop_size)
        assert crop_size[0] > 0 and crop_size[1] > 0
        self.crop_size = crop_size

    def __call__(self, results):

        crop_height, crop_width = self.crop_size[0], self.crop_size[1]
        for key in results.get("img_fields", ["img"]):
            img = results[key]
            # img.shape has length 2 for grayscale, length 3 for color
            img_height, img_width = img.shape[:2]

            y1 = max(0, int(round((img_height - crop_height) / 2.0)))
            x1 = max(0, int(round((img_width - crop_width) / 2.0)))
            y2 = min(img_height, y1 + crop_height) - 1
            x2 = min(img_width, x1 + crop_width) - 1

            # crop the image
            img = imcrop(img, bboxes=np.array([x1, y1, x2, y2]))
            img_shape = img.shape
            results[key] = img
        results["img_shape"] = img_shape

        return results

    def __repr__(self):

        return self.__class__.__name__ + f"(crop_size={self.crop_size})"


@PIPELINES.register_module()
class SegRescale:
    """Rescale semantic segmentation maps."""

    def __init__(self, scale_factor=1, backend="cv2"):
        """Initialization of rescale for segmentation

        Args:
            scale_factor (float): The scale factor of the final output.
            backend (str): Image rescale backend, choices are 'cv2' and 'pillow'.
                These two backends generates slightly different results. Defaults
                to 'cv2'.
        """

        self.scale_factor = scale_factor
        self.backend = backend

    def __call__(self, results):
        """Call function to scale the semantic segmentation map.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with semantic segmentation map scaled.
        """

        for key in results.get("seg_fields", []):
            if self.scale_factor != 1:
                results[key] = imrescale(
                    results[key],
                    self.scale_factor,
                    interpolation="nearest",
                    backend=self.backend,
                )
        return results

    def __repr__(self):
        return self.__class__.__name__ + f"(scale_factor={self.scale_factor})"


@PIPELINES.register_module()
class GenerateHeatMap:
    """Generate heat maps for segmentation."""

    def __init__(self, sigma=3, range=20):
        """Initialization of rescale for segmentation

        Args:
            sigma (float): The scale factor of the final output.
            range (str): Image rescale backend, choices are 'cv2' and 'pillow'.
                These two backends generates slightly different results. Defaults
                to 'cv2'.
        """

        self.sigma = sigma
        self.range = range

    def __call__(self, results):
        """Call function to scale the semantic segmentation map.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with semantic segmentation map scaled.
        """
        # 3-sigma rule
        tmp_size = self.sigma * 3

        num_class = 2

        H = results["img_shape"][0]
        W = results["img_shape"][1]

        results["gt_semantic_seg"] = np.zeros((num_class, H, W), dtype=np.float32)
        results["seg_fields"].append("gt_semantic_seg")

        if results["ann_info"]["concat_type"] == 0:  # split with horizontal line

            feat_stride = results["ori_shape"][0] / H
            for concat_line in results["ann_info"]["concat_lines"]:
                split_y = int(concat_line / feat_stride + 0.5)

                if split_y < 0 or split_y >= H:
                    continue

                min_y = int(split_y - tmp_size)
                max_y = int(split_y + tmp_size + 1)

                size = 2 * tmp_size + 1
                y = np.arange(0, size, 1, np.float32)
                y0 = size // 2
                # The gaussian is not normalized,
                # we want the center value to equal 1
                g = np.exp(-((y - y0) ** 2) / (2 * self.sigma ** 2))

                # Usable gaussian range
                g_y = max(0, -min_y), min(max_y, H) - min_y
                # Image range
                img_y = max(0, min_y), min(max_y, H)

                for i in range(W):
                    results["gt_semantic_seg"][0, img_y[0] : img_y[1], i] += g[
                        g_y[0] : g_y[1]
                    ]

        else:  # split with vertical line
            feat_stride = results["ori_shape"][1] / W

            for concat_line in results["ann_info"]["concat_lines"]:
                split_x = int(concat_line / feat_stride + 0.5)

                if split_x < 0 or split_x >= W:
                    continue

                min_x = int(split_x - tmp_size)
                max_x = int(split_x + tmp_size + 1)

                size = 2 * tmp_size + 1
                x = np.arange(0, size, 1, np.float32)
                x0 = size // 2
                # The gaussian is not normalized,
                # we want the center value to equal 1
                g = np.exp(-((x - x0) ** 2) / (2 * self.sigma ** 2))

                # Usable gaussian range
                g_x = max(0, -min_x), min(max_x, W) - min_x
                # Image range
                img_x = max(0, min_x), min(max_x, W)

                for i in range(H):
                    results["gt_semantic_seg"][1, i, img_x[0] : img_x[1]] += g[
                        g_x[0] : g_x[1]
                    ]

        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f"(sigma={self.sigma})"
        repr_str += f"(range={self.range})"
        return repr_str


@PIPELINES.register_module()
class PhotoMetricDistortion:
    """Apply photometric distortion to image sequentially, every transformation
    is applied with a probability of 0.5. The position of random contrast is in
    second or second to last.

    1. random brightness
    2. random contrast (mode 0)
    3. convert color from BGR to HSV
    4. random saturation
    5. random hue
    6. convert color from HSV to BGR
    7. random contrast (mode 1)
    8. randomly swap channels
    """

    def __init__(
        self,
        brightness_delta=32,
        contrast_range=(0.5, 1.5),
        saturation_range=(0.5, 1.5),
        hue_delta=18,
    ):
        """Initialization for photo metric distortion.

        Args:
            brightness_delta (int): delta of brightness.
            contrast_range (tuple): range of contrast.
            saturation_range (tuple): range of saturation.
            hue_delta (int): delta of hue.
        """
        self.brightness_delta = brightness_delta
        self.contrast_lower, self.contrast_upper = contrast_range
        self.saturation_lower, self.saturation_upper = saturation_range
        self.hue_delta = hue_delta

    def __call__(self, results):
        """Call function to perform photometric distortion on images.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with images distorted.
        """

        if "img_fields" in results:
            assert results["img_fields"] == ["img"], "Only single img_fields is allowed"
        img = results["img"]
        assert img.dtype == np.float32, (
            "PhotoMetricDistortion needs the input image of dtype np.float32,"
            ' please set "to_float32=True" in "LoadImageFromFile" pipeline'
        )

        # random brightness
        if np.random.randint(2):
            delta = random.uniform(-self.brightness_delta, self.brightness_delta)
            img += delta

        # mode == 0 --> do random contrast first
        # mode == 1 --> do random contrast last
        mode = np.random.randint(2)
        if mode == 1:
            if np.random.randint(2):
                alpha = np.random.uniform(self.contrast_lower, self.contrast_upper)
                img *= alpha

        # convert color from BGR to HSV
        img = bgr2hsv(img)

        # random saturation
        if np.random.randint(2):
            img[..., 1] *= np.random.uniform(
                self.saturation_lower, self.saturation_upper
            )

        # random hue
        if np.random.randint(2):
            img[..., 0] += np.random.uniform(-self.hue_delta, self.hue_delta)
            img[..., 0][img[..., 0] > 360] -= 360
            img[..., 0][img[..., 0] < 0] += 360

        # convert color from HSV to BGR
        img = hsv2bgr(img)

        # random contrast
        if mode == 0:
            if np.random.randint(2):
                alpha = np.random.uniform(self.contrast_lower, self.contrast_upper)
                img *= alpha

        # randomly swap channels
        if np.random.randint(2):
            img = img[..., np.random.permutation(3)]

        results["img"] = img
        return results

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(\nbrightness_delta={self.brightness_delta},\n"
        repr_str += "contrast_range="
        repr_str += f"{(self.contrast_lower, self.contrast_upper)},\n"
        repr_str += "saturation_range="
        repr_str += f"{(self.saturation_lower, self.saturation_upper)},\n"
        repr_str += f"hue_delta={self.hue_delta})"
        return repr_str


@PIPELINES.register_module()
class Expand:
    """Random expand the image & bboxes.
    Randomly place the original image on a canvas of 'ratio' x original image
    size filled with mean values. The ratio is in the range of ratio_range.
    """

    def __init__(
        self,
        mean=(0, 0, 0),
        to_rgb=True,
        ratio_range=(1, 4),
        seg_ignore_label=None,
        prob=0.5,
    ):
        """Initialization for expanding the image and bboxes

        Args:
            mean (tuple): mean value of dataset.
            to_rgb (bool): if need to convert the order of mean to align with RGB.
            ratio_range (tuple): range of expand ratio.
            prob (float): probability of applying this transformation
        """

        self.to_rgb = to_rgb
        self.ratio_range = ratio_range
        if to_rgb:
            self.mean = mean[::-1]
        else:
            self.mean = mean
        self.min_ratio, self.max_ratio = ratio_range
        self.seg_ignore_label = seg_ignore_label
        self.prob = prob

    def __call__(self, results):
        """Call function to expand images, bounding boxes.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with images, bounding boxes expanded
        """

        if np.random.uniform(0, 1) > self.prob:
            return results

        if "img_fields" in results:
            assert results["img_fields"] == ["img"], "Only single img_fields is allowed"
        img = results["img"]

        h, w, c = img.shape
        ratio = np.random.uniform(self.min_ratio, self.max_ratio)
        expand_img = np.full(
            (int(h * ratio), int(w * ratio), c), self.mean, dtype=img.dtype
        )
        left = int(np.random.uniform(0, w * ratio - w))
        top = int(np.random.uniform(0, h * ratio - h))
        expand_img[top : top + h, left : left + w] = img

        results["img"] = expand_img
        # expand bboxes
        for key in results.get("bbox_fields", []):
            results[key] = results[key] + np.tile((left, top), 2).astype(
                results[key].dtype
            )

        # expand masks
        for key in results.get("mask_fields", []):
            results[key] = results[key].expand(
                int(h * ratio), int(w * ratio), top, left
            )

        # expand segs
        for key in results.get("seg_fields", []):
            gt_seg = results[key]
            expand_gt_seg = np.full(
                (int(h * ratio), int(w * ratio)),
                self.seg_ignore_label,
                dtype=gt_seg.dtype,
            )
            expand_gt_seg[top : top + h, left : left + w] = gt_seg
            results[key] = expand_gt_seg
        return results

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(mean={self.mean}, to_rgb={self.to_rgb}, "
        repr_str += f"ratio_range={self.ratio_range}, "
        repr_str += f"seg_ignore_label={self.seg_ignore_label})"
        return repr_str


@PIPELINES.register_module()
class MinIoURandomCrop:
    """Random crop the image & bboxes, the cropped patches have minimum IoU
    requirement with original image & bboxes, the IoU threshold is randomly
    selected from min_ious.
    """

    def __init__(self, min_ious=[0.1, 0.3, 0.5, 0.7, 0.9], min_crop_size=0.3):
        """Initialization for random crop with min iou.

        Args:
            min_ious (tuple): minimum IoU threshold for all intersections with
            bounding boxes
            min_crop_size (float): minimum crop's size (i.e. h,w := a*h, a*w,
            where a >= min_crop_size).

        Note:
            The keys for bboxes, labels and masks should be paired. That is, \
            `gt_bboxes` corresponds to `gt_labels` and `gt_masks`, and \
            `gt_bboxes_ignore` to `gt_labels_ignore` and `gt_masks_ignore`.
        """

        # 1: return ori img
        self.min_ious = min_ious
        self.sample_mode = (1, *min_ious, 0)
        self.min_crop_size = min_crop_size
        self.bbox2label = {
            "gt_bboxes": "gt_labels",
            "gt_bboxes_ignore": "gt_labels_ignore",
        }
        self.bbox2mask = {
            "gt_bboxes": "gt_masks",
            "gt_bboxes_ignore": "gt_masks_ignore",
        }

    def __call__(self, results):
        """Call function to crop images and bounding boxes with minimum IoU
        constraint.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with images and bounding boxes cropped, \
                'img_shape' key is updated.
        """

        if "img_fields" in results:
            assert results["img_fields"] == ["img"], "Only single img_fields is allowed"
        img = results["img"]
        assert "bbox_fields" in results
        boxes = [results[key] for key in results["bbox_fields"]]
        boxes = np.concatenate(boxes, 0)
        h, w, c = img.shape
        while True:
            mode = np.random.choice(self.sample_mode)
            self.mode = mode
            if mode == 1:
                return results

            min_iou = mode
            for i in range(50):
                new_w = np.random.uniform(self.min_crop_size * w, w)
                new_h = np.random.uniform(self.min_crop_size * h, h)

                # h / w in [0.5, 2]
                if new_h / new_w < 0.5 or new_h / new_w > 2:
                    continue

                left = np.random.uniform(w - new_w)
                top = np.random.uniform(h - new_h)

                patch = np.array(
                    (int(left), int(top), int(left + new_w), int(top + new_h))
                )
                # Line or point crop is not allowed
                if patch[2] == patch[0] or patch[3] == patch[1]:
                    continue
                overlaps = bbox_overlaps_np(
                    patch.reshape(-1, 4), boxes.reshape(-1, 4)
                ).reshape(-1)
                if len(overlaps) > 0 and overlaps.min() < min_iou:
                    continue

                # center of boxes should inside the crop img
                # only adjust boxes and instance masks when the gt is not empty
                if len(overlaps) > 0:
                    # adjust boxes
                    def is_center_of_bboxes_in_patch(boxes, patch):
                        center = (boxes[:, :2] + boxes[:, 2:]) / 2
                        mask = (
                            (center[:, 0] > patch[0])
                            * (center[:, 1] > patch[1])
                            * (center[:, 0] < patch[2])
                            * (center[:, 1] < patch[3])
                        )
                        return mask

                    mask = is_center_of_bboxes_in_patch(boxes, patch)
                    if not mask.any():
                        continue
                    for key in results.get("bbox_fields", []):
                        boxes = results[key].copy()
                        mask = is_center_of_bboxes_in_patch(boxes, patch)
                        boxes = boxes[mask]
                        boxes[:, 2:] = boxes[:, 2:].clip(max=patch[2:])
                        boxes[:, :2] = boxes[:, :2].clip(min=patch[:2])
                        boxes -= np.tile(patch[:2], 2)

                        results[key] = boxes
                        # labels
                        label_key = self.bbox2label.get(key)
                        if label_key in results:
                            results[label_key] = results[label_key][mask]

                        # mask fields
                        mask_key = self.bbox2mask.get(key)
                        if mask_key in results:
                            results[mask_key] = results[mask_key][
                                mask.nonzero()[0]
                            ].crop(patch)
                # adjust the img no matter whether the gt is empty before crop
                img = img[patch[1] : patch[3], patch[0] : patch[2]]
                results["img"] = img
                results["img_shape"] = img.shape

                # seg fields
                for key in results.get("seg_fields", []):
                    results[key] = results[key][
                        patch[1] : patch[3], patch[0] : patch[2]
                    ]
                return results

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(min_ious={self.min_ious}, "
        repr_str += f"min_crop_size={self.min_crop_size})"
        return repr_str


@PIPELINES.register_module()
class Corrupt:
    """Corruption augmentation.
    Corruption transforms implemented based on
    `imagecorruptions <https://github.com/bethgelab/imagecorruptions>`_.
    """

    def __init__(self, corruption, severity=1):
        """Initialization for corrupt.

        Args:
            corruption (str): Corruption name.
            severity (int, optional): The severity of corruption. Default: 1.
        """
        self.corruption = corruption
        self.severity = severity

    def __call__(self, results):
        """Call function to corrupt image.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with images corrupted.
        """

        if corrupt is None:
            raise RuntimeError("imagecorruptions is not installed")
        if "img_fields" in results:
            assert results["img_fields"] == ["img"], "Only single img_fields is allowed"
        results["img"] = corrupt(
            results["img"].astype(np.uint8),
            corruption_name=self.corruption,
            severity=self.severity,
        )
        return results

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(corruption={self.corruption}, "
        repr_str += f"severity={self.severity})"
        return repr_str


@PIPELINES.register_module()
class Albu:
    """Albumentation augmentation.
    Adds custom transformations from Albumentations library.
    Please, visit `https://albumentations.readthedocs.io`
    to get more information.

    An example of ``transforms`` is as followed:
    .. code-block::
        [
            dict(
                type='ShiftScaleRotate',
                shift_limit=0.0625,
                scale_limit=0.0,
                rotate_limit=0,
                interpolation=1,
                p=0.5),
            dict(
                type='RandomBrightnessContrast',
                brightness_limit=[0.1, 0.3],
                contrast_limit=[0.1, 0.3],
                p=0.2),
            dict(type='ChannelShuffle', p=0.1),
            dict(
                type='OneOf',
                transforms=[
                    dict(type='Blur', blur_limit=3, p=1.0),
                    dict(type='MedianBlur', blur_limit=3, p=1.0)
                ],
                p=0.1),
        ]
    """

    def __init__(
        self,
        transforms,
        bbox_params=None,
        keymap=None,
        update_pad_shape=False,
        skip_img_without_anno=False,
    ):
        """Initialization for albu augmentation.

        Args:
            transforms (list[dict]): A list of albu transformations
            bbox_params (dict): Bbox_params for albumentation `Compose`
            keymap (dict): Contains {'input key':'albumentation-style key'}
            skip_img_without_anno (bool): Whether to skip the image if no ann left
                after aug
        """

        if Compose is None:
            raise RuntimeError("albumentations is not installed")

        self.transforms = transforms
        self.filter_lost_elements = False
        self.update_pad_shape = update_pad_shape
        self.skip_img_without_anno = skip_img_without_anno

        # A simple workaround to remove masks without boxes
        if (
            isinstance(bbox_params, dict)
            and "label_fields" in bbox_params
            and "filter_lost_elements" in bbox_params
        ):
            self.filter_lost_elements = True
            self.origin_label_fields = bbox_params["label_fields"]
            bbox_params["label_fields"] = ["idx_mapper"]
            del bbox_params["filter_lost_elements"]

        self.bbox_params = self.albu_builder(bbox_params) if bbox_params else None
        self.aug = Compose(
            [self.albu_builder(t) for t in self.transforms],
            bbox_params=self.bbox_params,
        )

        if not keymap:
            self.keymap_to_albu = {
                "img": "image",
                "gt_masks": "masks",
                "gt_bboxes": "bboxes",
            }
        else:
            self.keymap_to_albu = keymap
        self.keymap_back = {v: k for k, v in self.keymap_to_albu.items()}

    def albu_builder(self, cfg):
        """Import a module from albumentations.
        It inherits some of :func:`build_from_cfg` logic.

        Args:
            cfg (dict): Config dict. It should at least contain the key "type".

        Returns:
            obj: The constructed object.
        """

        assert isinstance(cfg, dict) and "type" in cfg
        args = cfg.copy()

        obj_type = args.pop("type")
        if is_str(obj_type):
            if albumentations is None:
                raise RuntimeError("albumentations is not installed")
            obj_cls = getattr(albumentations, obj_type)
        elif inspect.isclass(obj_type):
            obj_cls = obj_type
        else:
            raise TypeError(
                f"type must be a str or valid type, but got {type(obj_type)}"
            )

        if "transforms" in args:
            args["transforms"] = [
                self.albu_builder(transform) for transform in args["transforms"]
            ]

        return obj_cls(**args)

    @staticmethod
    def mapper(d, keymap):
        """Dictionary mapper. Renames keys according to keymap provided.

        Args:
            d (dict): old dict
            keymap (dict): {'old_key':'new_key'}

        Returns:
            dict: new dict.
        """

        updated_dict = {}
        for k, v in zip(d.keys(), d.values()):
            new_k = keymap.get(k, k)
            updated_dict[new_k] = d[k]
        return updated_dict

    def __call__(self, results):
        # dict to albumentations format

        results = self.mapper(results, self.keymap_to_albu)
        # TODO: add bbox_fields
        if "bboxes" in results:
            # to list of boxes
            if isinstance(results["bboxes"], np.ndarray):
                results["bboxes"] = [x for x in results["bboxes"]]
            # add pseudo-field for filtration
            if self.filter_lost_elements:
                results["idx_mapper"] = np.arange(len(results["bboxes"]))

        # TODO: Support mask structure in albu
        if "masks" in results:
            if isinstance(results["masks"], PolygonMasks):
                raise NotImplementedError("Albu only supports BitMap masks now")
            ori_masks = results["masks"]
            results["masks"] = results["masks"].masks

        results = self.aug(**results)

        if "bboxes" in results:
            if isinstance(results["bboxes"], list):
                results["bboxes"] = np.array(results["bboxes"], dtype=np.float32)
            results["bboxes"] = results["bboxes"].reshape(-1, 4)

            # filter label_fields
            if self.filter_lost_elements:

                for label in self.origin_label_fields:
                    results[label] = np.array(
                        [results[label][i] for i in results["idx_mapper"]]
                    )
                if "masks" in results:
                    results["masks"] = np.array(
                        [results["masks"][i] for i in results["idx_mapper"]]
                    )
                    results["masks"] = ori_masks.__class__(
                        results["masks"],
                        results["image"].shape[0],
                        results["image"].shape[1],
                    )

                if not len(results["idx_mapper"]) and self.skip_img_without_anno:
                    return None

        if "gt_labels" in results:
            if isinstance(results["gt_labels"], list):
                results["gt_labels"] = np.array(results["gt_labels"])
            results["gt_labels"] = results["gt_labels"].astype(np.int64)

        # back to the original format
        results = self.mapper(results, self.keymap_back)

        # update final shape
        if self.update_pad_shape:
            results["pad_shape"] = results["img"].shape

        return results

    def __repr__(self):
        repr_str = self.__class__.__name__ + f"(transforms={self.transforms})"
        return repr_str


@PIPELINES.register_module()
class RandomCenterCropPad:
    """Random center crop and random around padding for CornerNet.
    This operation generates randomly cropped image from the original image and
    pads it simultaneously. Different from :class:`RandomCrop`, the output
    shape may not equal to ``crop_size`` strictly. We choose a random value
    from ``ratios`` and the output shape could be larger or smaller than
    ``crop_size``. The padding operation is also different from :class:`Pad`,
    here we use around padding instead of right-bottom padding.

    The relation between output image (padding image) and original image:
    .. code:: text
                        output image

               +----------------------------+
               |          padded area       |
        +------|----------------------------|----------+
        |      |         cropped area       |          |
        |      |         +---------------+  |          |
        |      |         |    .   center |  |          | original image
        |      |         |        range  |  |          |
        |      |         +---------------+  |          |
        +------|----------------------------|----------+
               |          padded area       |
               +----------------------------+

    There are 5 main areas in the figure:
    - output image: output image of this operation, also called padding
      image in following instruction.
    - original image: input image of this operation.
    - padded area: non-intersect area of output image and original image.
    - cropped area: the overlap of output image and original image.
    - center range: a smaller area where random center chosen from.
      center range is computed by ``border`` and original image's shape
      to avoid our random center is too close to original image's border.
    Also this operation act differently in train and test mode, the summary
    pipeline is listed below.
    Train pipeline:
    1. Choose a ``random_ratio`` from ``ratios``, the shape of padding image
       will be ``random_ratio * crop_size``.
    2. Choose a ``random_center`` in center range.
    3. Generate padding image with center matches the ``random_center``.
    4. Initialize the padding image with pixel value equals to ``mean``.
    5. Copy the cropped area to padding image.
    6. Refine annotations.
    Test pipeline:
    1. Compute output shape according to ``test_pad_mode``.
    2. Generate padding image with center matches the original image
       center.
    3. Initialize the padding image with pixel value equals to ``mean``.
    4. Copy the ``cropped area`` to padding image.
    """

    def __init__(
        self,
        crop_size=None,
        ratios=(0.9, 1.0, 1.1),
        border=128,
        mean=None,
        std=None,
        to_rgb=None,
        test_mode=False,
        test_pad_mode=("logical_or", 127),
    ):
        """
        Args:
            crop_size (tuple | None): expected size after crop, final size will
                computed according to ratio. Requires (h, w) in train mode, and
                None in test mode.
            ratios (tuple): random select a ratio from tuple and crop image to
                (crop_size[0] * ratio) * (crop_size[1] * ratio).
                Only available in train mode.
            border (int): max distance from center select area to image border.
                Only available in train mode.
            mean (sequence): Mean values of 3 channels.
            std (sequence): Std values of 3 channels.
            to_rgb (bool): Whether to convert the image from BGR to RGB.
            test_mode (bool): whether involve random variables in transform.
                In train mode, crop_size is fixed, center coords and ratio is
                random selected from predefined lists. In test mode, crop_size
                is image's original shape, center coords and ratio is fixed.
            test_pad_mode (tuple): padding method and padding shape value, only
                available in test mode. Default is using 'logical_or' with
                127 as padding shape value.
                - 'logical_or': final_shape = input_shape | padding_shape_value
                - 'size_divisor': final_shape = int(
                ceil(input_shape / padding_shape_value) * padding_shape_value)
        """

        if test_mode:
            assert crop_size is None, "crop_size must be None in test mode"
            assert ratios is None, "ratios must be None in test mode"
            assert border is None, "border must be None in test mode"
            assert isinstance(test_pad_mode, (list, tuple))
            assert test_pad_mode[0] in ["logical_or", "size_divisor"]
        else:
            assert isinstance(crop_size, (list, tuple))
            assert (
                crop_size[0] > 0 and crop_size[1] > 0
            ), "crop_size must > 0 in train mode"
            assert isinstance(ratios, (list, tuple))
            assert test_pad_mode is None, "test_pad_mode must be None in train mode"

        self.crop_size = crop_size
        self.ratios = ratios
        self.border = border
        # We do not set default value to mean, std and to_rgb because these
        # hyper-parameters are easy to forget but could affect the performance.
        # Please use the same setting as Normalize for performance assurance.
        assert mean is not None and std is not None and to_rgb is not None
        self.to_rgb = to_rgb
        self.input_mean = mean
        self.input_std = std
        if to_rgb:
            self.mean = mean[::-1]
            self.std = std[::-1]
        else:
            self.mean = mean
            self.std = std
        self.test_mode = test_mode
        self.test_pad_mode = test_pad_mode

    def _get_border(self, border, size):
        """Get final border for the target size.
        This function generates a ``final_border`` according to image's shape.
        The area between ``final_border`` and ``size - final_border`` is the
        ``center range``. We randomly choose center from the ``center range``
        to avoid our random center is too close to original image's border.
        Also ``center range`` should be larger than 0.

        Args:
            border (int): The initial border, default is 128.
            size (int): The width or height of original image.
        Returns:
            int: The final border.
        """

        k = 2 * border / size
        i = pow(2, np.ceil(np.log2(np.ceil(k))) + (k == int(k)))
        return border // i

    def _filter_boxes(self, patch, boxes):
        """Check whether the center of each box is in the patch.

        Args:
            patch (list[int]): The cropped area, [left, top, right, bottom].
            boxes (numpy array, (N x 4)): Ground truth boxes.

        Returns:
            mask (numpy array, (N,)): Each box is inside or outside the patch.
        """

        center = (boxes[:, :2] + boxes[:, 2:]) / 2
        mask = (
            (center[:, 0] > patch[0])
            * (center[:, 1] > patch[1])
            * (center[:, 0] < patch[2])
            * (center[:, 1] < patch[3])
        )
        return mask

    def _crop_image_and_paste(self, image, center, size):
        """Crop image with a given center and size, then paste the cropped
        image to a blank image with two centers align.
        This function is equivalent to generating a blank image with ``size``
        as its shape. Then cover it on the original image with two centers (
        the center of blank image and the random center of original image)
        aligned. The overlap area is paste from the original image and the
        outside area is filled with ``mean pixel``.

        Args:
            image (np array, H x W x C): Original image.
            center (list[int]): Target crop center coord.
            size (list[int]): Target crop size. [target_h, target_w]

        Returns:
            cropped_img (np array, target_h x target_w x C): Cropped image.
            border (np array, 4): The distance of four border of
                ``cropped_img`` to the original image area, [top, bottom,
                left, right]
            patch (list[int]): The cropped area, [left, top, right, bottom].
        """

        center_y, center_x = center
        target_h, target_w = size
        img_h, img_w, img_c = image.shape

        x0 = max(0, center_x - target_w // 2)
        x1 = min(center_x + target_w // 2, img_w)
        y0 = max(0, center_y - target_h // 2)
        y1 = min(center_y + target_h // 2, img_h)
        patch = np.array((int(x0), int(y0), int(x1), int(y1)))

        left, right = center_x - x0, x1 - center_x
        top, bottom = center_y - y0, y1 - center_y

        cropped_center_y, cropped_center_x = target_h // 2, target_w // 2
        cropped_img = np.zeros((target_h, target_w, img_c), dtype=image.dtype)
        for i in range(img_c):
            cropped_img[:, :, i] += self.mean[i]
        y_slice = slice(cropped_center_y - top, cropped_center_y + bottom)
        x_slice = slice(cropped_center_x - left, cropped_center_x + right)
        cropped_img[y_slice, x_slice, :] = image[y0:y1, x0:x1, :]

        border = np.array(
            [
                cropped_center_y - top,
                cropped_center_y + bottom,
                cropped_center_x - left,
                cropped_center_x + right,
            ],
            dtype=np.float32,
        )

        return cropped_img, border, patch

    def _train_aug(self, results):
        """Random crop and around padding the original image.

        Args:
            results (dict): Image infomations in the augment pipeline.

        Returns:
            results (dict): The updated dict.
        """

        img = results["img"]
        h, w, c = img.shape
        boxes = results["gt_bboxes"]
        while True:
            scale = np.random.choice(self.ratios)
            new_h = int(self.crop_size[0] * scale)
            new_w = int(self.crop_size[1] * scale)
            h_border = self._get_border(self.border, h)
            w_border = self._get_border(self.border, w)

            for i in range(50):
                center_x = np.random.randint(low=w_border, high=w - w_border)
                center_y = np.random.randint(low=h_border, high=h - h_border)

                cropped_img, border, patch = self._crop_image_and_paste(
                    img, [center_y, center_x], [new_h, new_w]
                )

                mask = self._filter_boxes(patch, boxes)
                # if image do not have valid bbox, any crop patch is valid.
                if not mask.any() and len(boxes) > 0:
                    continue

                results["img"] = cropped_img
                results["img_shape"] = cropped_img.shape
                results["pad_shape"] = cropped_img.shape

                x0, y0, x1, y1 = patch

                left_w, top_h = center_x - x0, center_y - y0
                cropped_center_x, cropped_center_y = new_w // 2, new_h // 2

                # crop bboxes accordingly and clip to the image boundary
                for key in results.get("bbox_fields", []):
                    mask = self._filter_boxes(patch, results[key])
                    bboxes = results[key][mask]
                    bboxes[:, 0:4:2] += cropped_center_x - left_w - x0
                    bboxes[:, 1:4:2] += cropped_center_y - top_h - y0
                    bboxes[:, 0:4:2] = np.clip(bboxes[:, 0:4:2], 0, new_w)
                    bboxes[:, 1:4:2] = np.clip(bboxes[:, 1:4:2], 0, new_h)
                    keep = (bboxes[:, 2] > bboxes[:, 0]) & (bboxes[:, 3] > bboxes[:, 1])
                    bboxes = bboxes[keep]
                    results[key] = bboxes
                    if key in ["gt_bboxes"]:
                        if "gt_labels" in results:
                            labels = results["gt_labels"][mask]
                            labels = labels[keep]
                            results["gt_labels"] = labels
                        if "gt_masks" in results:
                            raise NotImplementedError(
                                "RandomCenterCropPad only supports bbox."
                            )

                # crop semantic seg
                for key in results.get("seg_fields", []):
                    raise NotImplementedError("RandomCenterCropPad only supports bbox.")
                return results

    def _test_aug(self, results):
        """Around padding the original image without cropping.

        The padding mode and value are from ``test_pad_mode``.

        Args:
            results (dict): Image infomations in the augment pipeline.

        Returns:
            results (dict): The updated dict.
        """

        img = results["img"]
        h, w, c = img.shape
        results["img_shape"] = img.shape
        if self.test_pad_mode[0] in ["logical_or"]:
            target_h = h | self.test_pad_mode[1]
            target_w = w | self.test_pad_mode[1]
        elif self.test_pad_mode[0] in ["size_divisor"]:
            divisor = self.test_pad_mode[1]
            target_h = int(np.ceil(h / divisor)) * divisor
            target_w = int(np.ceil(w / divisor)) * divisor
        else:
            raise NotImplementedError(
                "RandomCenterCropPad only support two testing pad mode:"
                "logical-or and size_divisor."
            )

        cropped_img, border, _ = self._crop_image_and_paste(
            img, [h // 2, w // 2], [target_h, target_w]
        )
        results["img"] = cropped_img
        results["pad_shape"] = cropped_img.shape
        results["border"] = border
        return results

    def __call__(self, results):

        img = results["img"]
        assert img.dtype == np.float32, (
            "RandomCenterCropPad needs the input image of dtype np.float32,"
            ' please set "to_float32=True" in "LoadImageFromFile" pipeline'
        )
        h, w, c = img.shape
        assert c == len(self.mean)
        if self.test_mode:
            return self._test_aug(results)
        else:
            return self._train_aug(results)

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(crop_size={self.crop_size}, "
        repr_str += f"ratios={self.ratios}, "
        repr_str += f"border={self.border}, "
        repr_str += f"mean={self.input_mean}, "
        repr_str += f"std={self.input_std}, "
        repr_str += f"to_rgb={self.to_rgb}, "
        repr_str += f"test_mode={self.test_mode}, "
        repr_str += f"test_pad_mode={self.test_pad_mode})"
        return repr_str


@PIPELINES.register_module()
class CutOut:
    """CutOut operation.
    Randomly drop some regions of image used in
    `Cutout <https://arxiv.org/abs/1708.04552>`_.
    """

    def __init__(
        self, n_holes, cutout_shape=None, cutout_ratio=None, fill_in=(0, 0, 0)
    ):
        """Initialization for cutout.

        Args:
            n_holes (int | tuple[int, int]): Number of regions to be dropped.
                If it is given as a list, number of holes will be randomly
                selected from the closed interval [`n_holes[0]`, `n_holes[1]`].
            cutout_shape (tuple[int, int] | list[tuple[int, int]]): The candidate
                shape of dropped regions. It can be `tuple[int, int]` to use a
                fixed cutout shape, or `list[tuple[int, int]]` to randomly choose
                shape from the list.
            cutout_ratio (tuple[float, float] | list[tuple[float, float]]): The
                candidate ratio of dropped regions. It can be `tuple[float, float]`
                to use a fixed ratio or `list[tuple[float, float]]` to randomly
                choose ratio from the list. Please note that `cutout_shape`
                and `cutout_ratio` cannot be both given at the same time.
            fill_in (tuple[float, float, float] | tuple[int, int, int]): The value
                of pixel to fill in the dropped regions. Default: (0, 0, 0).
        """

        assert (cutout_shape is None) ^ (
            cutout_ratio is None
        ), "Either cutout_shape or cutout_ratio should be specified."
        assert isinstance(cutout_shape, (list, tuple)) or isinstance(
            cutout_ratio, (list, tuple)
        )
        if isinstance(n_holes, tuple):
            assert len(n_holes) == 2 and 0 <= n_holes[0] < n_holes[1]
        else:
            n_holes = (n_holes, n_holes)
        self.n_holes = n_holes
        self.fill_in = fill_in
        self.with_ratio = cutout_ratio is not None
        self.candidates = cutout_ratio if self.with_ratio else cutout_shape
        if not isinstance(self.candidates, list):
            self.candidates = [self.candidates]

    def __call__(self, results):
        """Call function to drop some regions of image."""

        h, w, c = results["img"].shape
        n_holes = np.random.randint(self.n_holes[0], self.n_holes[1] + 1)
        for _ in range(n_holes):
            x1 = np.random.randint(0, w)
            y1 = np.random.randint(0, h)
            index = np.random.randint(0, len(self.candidates))
            if not self.with_ratio:
                cutout_w, cutout_h = self.candidates[index]
            else:
                cutout_w = int(self.candidates[index][0] * w)
                cutout_h = int(self.candidates[index][1] * h)

            x2 = np.clip(x1 + cutout_w, 0, w)
            y2 = np.clip(y1 + cutout_h, 0, h)
            results["img"][y1:y2, x1:x2, :] = self.fill_in

        return results

    def __repr__(self):

        repr_str = self.__class__.__name__
        repr_str += f"(n_holes={self.n_holes}, "
        repr_str += (
            f"cutout_ratio={self.candidates}, "
            if self.with_ratio
            else f"cutout_shape={self.candidates}, "
        )
        repr_str += f"fill_in={self.fill_in})"
        return repr_str


@PIPELINES.register_module()
class MosaicPipeline(object):
    def __init__(self, individual_pipeline, pad_val=0):
        self.individual_pipeline = PipelineCompose(individual_pipeline)
        self.pad_val = pad_val

    def __call__(self, results):
        input_results = results.copy()
        mosaic_results = [results]
        dataset = results["dataset"]
        # load another 3 images
        indices = dataset.batch_rand_others(results["_idx"], 3)
        for idx in indices:
            img_info = dataset.getitem_info(idx)
            ann_info = dataset.get_ann_info(idx)
            if "img" in img_info:
                _results = dict(
                    img_info=img_info, ann_info=ann_info, _idx=idx, img=img_info["img"]
                )
            else:
                _results = dict(img_info=img_info, ann_info=ann_info, _idx=idx)
            if dataset.proposals is not None:
                _results["proposals"] = dataset.proposals[idx]
            dataset.pre_pipeline(_results)
            mosaic_results.append(_results)

        for idx in range(4):
            mosaic_results[idx] = self.individual_pipeline(mosaic_results[idx])

        shapes = [results["pad_shape"] for results in mosaic_results]
        cxy = max(shapes[0][0], shapes[1][0], shapes[0][1], shapes[2][1])
        canvas_shape = (cxy * 2, cxy * 2, shapes[0][2])

        # base image with 4 tiles
        canvas = dict()
        for key in mosaic_results[0].get("img_fields", []):
            canvas[key] = np.full(canvas_shape, self.pad_val, dtype=np.uint8)
        for i, results in enumerate(mosaic_results):
            h, w = results["pad_shape"][:2]
            # place img in img4
            if i == 0:  # top left
                x1, y1, x2, y2 = cxy - w, cxy - h, cxy, cxy
            elif i == 1:  # top right
                x1, y1, x2, y2 = cxy, cxy - h, cxy + w, cxy
            elif i == 2:  # bottom left
                x1, y1, x2, y2 = cxy - w, cxy, cxy, cxy + h
            elif i == 3:  # bottom right
                x1, y1, x2, y2 = cxy, cxy, cxy + w, cxy + h

            for key in mosaic_results[0].get("img_fields", []):
                canvas[key][y1:y2, x1:x2] = results[key]

            for key in results.get("bbox_fields", []):
                bboxes = results[key]
                bboxes[:, 0::2] = bboxes[:, 0::2] + x1
                bboxes[:, 1::2] = bboxes[:, 1::2] + y1
                results[key] = bboxes

        output_results = input_results
        output_results["filename"] = None
        output_results["ori_filename"] = None
        output_results["img_fields"] = mosaic_results[0].get("img_fields", [])
        output_results["bbox_fields"] = mosaic_results[0].get("bbox_fields", [])
        for key in output_results["img_fields"]:
            output_results[key] = canvas[key]

        for key in output_results["bbox_fields"]:
            output_results[key] = np.concatenate(
                [r[key] for r in mosaic_results], axis=0
            )

        output_results["gt_labels"] = np.concatenate(
            [r["gt_labels"] for r in mosaic_results], axis=0
        )

        output_results["img_shape"] = canvas_shape
        output_results["ori_shape"] = canvas_shape
        output_results["flip"] = False
        output_results["flip_direction"] = None

        return output_results

    def __repr__(self):
        repr_str = (
            f"{self.__class__.__name__}("
            f"individual_pipeline={self.individual_pipeline}, "
            f"pad_val={self.pad_val})"
        )
        return repr_str


@PIPELINES.register_module()
class HueSaturationValueJitter(object):
    def __init__(self, hue_ratio=0.5, saturation_ratio=0.5, value_ratio=0.5):
        self.h_ratio = hue_ratio
        self.s_ratio = saturation_ratio
        self.v_ratio = value_ratio

    def __call__(self, results):
        for key in results.get("img_fields", []):
            results[key] = np.ascontiguousarray(results[key])
            img = results[key]
            # random gains
            r = (
                np.array([random.uniform(-1.0, 1.0) for _ in range(3)])
                * [self.h_ratio, self.s_ratio, self.v_ratio]
                + 1
            )
            hue, sat, val = cv2.split(cv2.cvtColor(img, cv2.COLOR_BGR2HSV))
            dtype = img.dtype  # uint8

            x = np.arange(0, 256, dtype=np.int16)
            lut_hue = ((x * r[0]) % 180).astype(dtype)
            lut_sat = np.clip(x * r[1], 0, 255).astype(dtype)
            lut_val = np.clip(x * r[2], 0, 255).astype(dtype)

            img_hsv = cv2.merge(
                (cv2.LUT(hue, lut_hue), cv2.LUT(sat, lut_sat), cv2.LUT(val, lut_val))
            ).astype(dtype)
            cv2.cvtColor(
                img_hsv, cv2.COLOR_HSV2BGR, dst=results[key]
            )  # no return needed
        return results

    def __repr__(self):
        repr_str = (
            f"{self.__class__.__name__}("
            f"hue_ratio={self.h_ratio}, "
            f"saturation_ratio={self.s_ratio}, "
            f"value_ratio={self.v_ratio})"
        )
        return repr_str


@PIPELINES.register_module()
class GtBBoxesFilter(object):
    def __init__(self, min_size=2, max_aspect_ratio=20):
        assert max_aspect_ratio > 1
        self.min_size = min_size
        self.max_aspect_ratio = max_aspect_ratio

    def __call__(self, results):
        bboxes = results["gt_bboxes"]
        labels = results["gt_labels"]
        w = bboxes[:, 2] - bboxes[:, 0]
        h = bboxes[:, 3] - bboxes[:, 1]
        ar = np.maximum(w / (h + 1e-16), h / (w + 1e-16))
        valid = (w > self.min_size) & (h > self.min_size) & (ar < self.max_aspect_ratio)
        results["gt_bboxes"] = bboxes[valid]
        results["gt_labels"] = labels[valid]
        return results

    def __repr__(self):
        repr_str = (
            f"{self.__class__.__name__}("
            f"min_size={self.min_size}, "
            f"max_aspect_ratio={self.max_aspect_ratio})"
        )
        return repr_str

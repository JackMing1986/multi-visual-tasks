from torch import nn

from mvt.blocks.block_builder import build_backbone, build_head, build_neck
from ..model_builder import EMBEDDERS
from .base_embedder import BaseEmbedder


@EMBEDDERS.register_module()
class ImgClsLocEmbedder(BaseEmbedder):
    def __init__(self, cfg):

        super(ImgClsLocEmbedder, self).__init__()
        self.type = cfg.TYPE
        self.backbone = build_backbone(cfg.BACKBONE)

        if len(cfg.NECK) > 0:
            self.neck = build_neck(cfg.NECK)

        self.head = build_head(cfg.EMB_HEAD)

        if "PRETRAINED_MODEL_PATH" in cfg:
            if cfg.PRETRAINED_MODEL_PATH != "":
                self.init_weights(pretrained=cfg.PRETRAINED_MODEL_PATH)
            else:
                self.init_weights()
        else:
            self.init_weights()

    def init_weights(self, pretrained=None):
        super(ImgClsLocEmbedder, self).init_weights(pretrained)
        self.backbone.init_weights(pretrained=pretrained)
        if self.with_neck:
            if isinstance(self.neck, nn.Sequential):
                for m in self.neck:
                    m.init_weights()
            else:
                self.neck.init_weights()
        self.head.init_weights()

    def extract_feat(self, img):
        """Directly extract features from the backbone + neck"""

        x = self.backbone(img)
        if self.with_neck:
            x = self.neck(x)
        return x

    def forward(self, img, label, bbox, return_loss=True, **kwargs):
        """
        Calls either forward_train or forward_test depending on whether
        return_loss=True. Note this setting will change the expected inputs.
        When `return_loss=True`, data_item and data_meta are single-nested (i.e.
        Tensor and List[dict]), and when `resturn_loss=False`, data_item and data_meta
        should be double nested (i.e.  List[Tensor], List[List[dict]]), with
        the outer list indicating test.
        """
        if return_loss:
            return self.forward_train(img, label, bbox, **kwargs)
        else:
            return self.forward_test(img, bbox, **kwargs)

    def forward_train(self, x, label, bbox, **kwargs):
        """Forward computation during training.

        Args:
            data_item (Tensor): of shape (N, C, K) encoding input clues.

            gt_bbox (Tensor): of shape (N, 4) encoding the ground-truth label
                of input images.

        Returns:
            dict[str, Tensor]: a dictionary of loss components
        """
        x = self.extract_feat(x)

        losses = dict()
        loss = self.head.forward_train(x, label, bbox)
        losses.update(loss)

        return losses

    def forward_test(self, x, bbox, **kwargs):
        """
        Args:
            datas (List[Tensor]): the outer list of Tensors in a batch.
        """

        return self.simple_test(x, bbox, **kwargs)

    def simple_test(self, x, bbox, **kwargs):
        """Test without augmentation."""
        x = self.extract_feat(x)
        return self.head.simple_test(x, bbox)

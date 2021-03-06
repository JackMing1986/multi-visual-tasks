import json
import os
import os.path as osp
import yaml

from mvt.utils.parallel_util import master_only
from mvt.utils.io_util import obj_dump
from ...core_hook import HOOKS
from .base import LoggerHook


@HOOKS.register_module()
class PaviLoggerHook(LoggerHook):
    def __init__(
        self,
        init_kwargs=None,
        add_graph=False,
        add_last_ckpt=False,
        interval=10,
        ignore_last=True,
        reset_flag=True,
        by_epoch=True,
    ):
        super(PaviLoggerHook, self).__init__(
            interval, ignore_last, reset_flag, by_epoch
        )
        self.init_kwargs = init_kwargs
        self.add_graph = add_graph
        self.add_last_ckpt = add_last_ckpt

    @master_only
    def before_run(self, runner):
        try:
            from pavi import SummaryWriter
        except ImportError:
            raise ImportError('Please run "pip install pavi" to install pavi.')

        self.run_name = runner.work_dir.split("/")[-1]

        if not self.init_kwargs:
            self.init_kwargs = dict()
        self.init_kwargs["task"] = self.run_name
        self.init_kwargs["model"] = runner._model_name
        if runner.meta is not None:
            if "config" in runner.meta:
                config_dict = runner.meta["config"]
                assert isinstance(config_dict, dict), (
                    'meta["config_dict"] has to be of a dict, '
                    f"but got {type(config_dict)}"
                )
            else:
                config_dict = None
            if config_dict is not None:
                # 'max_.*iter' is parsed in pavi sdk as the maximum iterations
                #  to properly set up the progress bar.
                config_dict = config_dict.copy()
                config_dict.setdefault("max_iter", runner.max_iters)
                # non-serializable values are first converted in
                # dump to json
                config_dict = json.loads(obj_dump(config_dict, file_format="json"))
                session_text = yaml.dump(config_dict)
                self.init_kwargs["session_text"] = session_text
        self.writer = SummaryWriter(**self.init_kwargs)

        if self.add_graph:
            self.writer.add_graph(runner.model)

    def get_step(self, runner):
        """Get the total training step/epoch."""
        if self.get_mode(runner) == "val" and self.by_epoch:
            return self.get_epoch(runner)
        else:
            return self.get_iter(runner)

    @master_only
    def log(self, runner):
        tags = self.get_loggable_tags(runner, add_mode=False)
        if tags:
            self.writer.add_scalars(self.get_mode(runner), tags, self.get_step(runner))

    @master_only
    def after_run(self, runner):
        if self.add_last_ckpt:
            ckpt_path = osp.join(runner.work_dir, "latest.pth")
            if osp.isfile(ckpt_path):
                ckpt_path = osp.join(runner.work_dir, os.readlink(ckpt_path))
                # runner.epoch += 1 has been done before `after_run`.
                iteration = runner.epoch if self.by_epoch else runner.iter
                return self.writer.add_snapshot_file(
                    tag=self.run_name, snapshot_file_path=ckpt_path, iteration=iteration
                )

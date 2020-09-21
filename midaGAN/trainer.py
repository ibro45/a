
import os
import sys
import logging
from pathlib import Path
import torch
from omegaconf import OmegaConf

from midaGAN.data import build_loader
from midaGAN.nn.gans import build_gan
from midaGAN.conf.builders import build_training_conf

from midaGAN.utils import communication, environment
from midaGAN.utils.logging.experiment_tracker import ExperimentTracker
from midaGAN.utils.summary import gan_summary

class Trainer():
    def __init__(self):
        self.logger = logging.getLogger(type(self).__name__)
        self.conf = build_training_conf()
        self._setup_logging()

        self.tracker = ExperimentTracker(self.conf)
        self.data_loader = build_loader(self.conf)
        self.model = build_gan(self.conf)

        start_iter = 1 if not self.conf.load_checkpoint else self.conf.load_checkpoint.count_start_iter
        end_iter = 1 + self.conf.n_iters + self.conf.n_iters_decay
        self.iters = range(start_iter, end_iter)
        self.iter_idx = 0
        self.checkpoint_freq = self.conf.logging.checkpoint_freq

    def run(self):
        if communication.is_main_process():
            self.logger.info(gan_summary(self.model, self.data_loader))
            self.logger.info('Training started.')

        self.tracker.start_dataloading_timer()
        for i, data in zip(self.iters, self.data_loader):
            self.tracker.start_computation_timer()
            self.tracker.end_dataloading_timer()
            self._set_iter_idx(i)
            
            self._do_iteration(data)
            self.tracker.end_computation_timer()
            
            learning_rates, losses, visuals = self.model.get_loggable_data()
            self.tracker.log_iter(learning_rates, losses, visuals)

            self._save_checkpoint()
            self._perform_scheduler_step()
            
            self.tracker.start_dataloading_timer()
        self.tracker.close()

    def _do_iteration(self, data):
        self.model.set_input(data)
        self.model.optimize_parameters()

    def _perform_scheduler_step(self):
        self.model.update_learning_rate()  # perform a scheduler step # TODO: better to make decaying rate in checkpoints rather than per iter

    def _save_checkpoint(self):
        # TODO: save on cancel
        if communication.is_main_process():
            if self.iter_idx % self.checkpoint_freq == 0:
                self.logger.info(f'Saving the model after {self.iter_idx} iterations.')
                self.model.save_checkpoint(self.iter_idx)

    def _set_iter_idx(self, iter_idx):
        self.iter_idx = iter_idx
        self.tracker.set_iter_idx(iter_idx)

    def _setup_logging(self, debug=False):
        # TODO: generalize for inference too?
        checkpoint_dir = self.conf.logging.checkpoint_dir
        filename = Path(checkpoint_dir) / 'log.txt'
        use_stdout = communication.get_local_rank() == 0 or debug
        log_level = 'INFO' if not debug else 'DEBUG'

        environment.setup_logging(use_stdout, filename, log_level=log_level)

        self.logger.info(f'Configuration: {OmegaConf.to_yaml(self.conf)}')
        self.logger.info(f'Saving checkpoints, logs and config to: {checkpoint_dir}')
        self.logger.info(f'Python version: {sys.version.strip()}')
        self.logger.info(f'PyTorch version: {torch.__version__}')  # noqa
        self.logger.info(f'CUDA {torch.version.cuda} - cuDNN {torch.backends.cudnn.version()}')
        
        # These two useful if we decide to keep logs of all processes
        #logger.info(f'Machine rank: {communication.get_rank()}.')  
        #logger.info(f'Local rank: {communication.get_local_rank()}.') 




    
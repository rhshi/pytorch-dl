import os, glob
import numpy as np
import torch
import gin


@gin.configurable(blacklist=['ckptdir'])
class Checkpointer():
    def __init__(self, ckptdir, max_ckpts_to_keep=None, min_ckpt_period=None, format='{:09d}'):
        self.ckptdir = ckptdir
        self.max_ckpts_to_keep = max_ckpts_to_keep
        self.min_ckpt_period = min_ckpt_period
        self.format = format
        os.makedirs(ckptdir, exist_ok=True)

    def ckpts(self):
        ckpts = glob.glob(os.path.join(self.ckptdir, "*.pt"))
        return sorted([int(c.split('/')[-1][:-3]) for c in ckpts])

    def get_ckpt_path(self, t):
        return os.path.join(self.ckptdir, self.format.format(t) + '.pt')

    def save(self, save_dict, t):
        ts = self.ckpts()
        max_t = max(ts) if len(ts) > 0 else -1
        assert t > max_t, f"Cannot save a checkpoint at timestep {t} when checkpoints at a later timestep exist."
        torch.save(save_dict, self.get_ckpt_path(t))
        self.prune_ckpts()

    def load(self, t=None):
        if t is None:
            t = max(self.ckpts())
        path = self.get_ckpt_path(t)
        assert os.path.exists(path), f"Can't find checkpoint at iteration {t}."
        if torch.cuda.is_available():
            return torch.load(path)
        else:
            return torch.load(path, map_location='cpu')

    def prune_ckpts(self):
        if self.max_ckpts_to_keep is None:
            return
        ts = np.sort(self.ckpts())
        if self.min_ckpt_period is None:
            ts_to_remove = ts[:-self.max_ckpts_to_keep]
        else:
            ckpt_period = [t // self.min_ckpt_period for t in ts]
            last_period = -1
            ts_to_remove = []
            for i, t in enumerate(ts):
                if ckpt_period[i] > last_period:
                    last_period = ckpt_period[i]
                elif (i + self.max_ckpts_to_keep) < len(ts):
                    ts_to_remove.append(t)

        for t in ts_to_remove:
            os.remove(self.get_ckpt_path(t))



"""
Unit Tests
"""

import unittest
from shutil import rmtree

class TestCheckpointer(unittest.TestCase):
    def test(self):
        ckptr = Checkpointer('./.test_ckpt_dir', max_ckpts_to_keep=2, min_ckpt_period=10)
        for t in range(100):
            ckptr.save({'test': t},  t)

        assert ckptr.load()['test'] == 99

        assert ckptr.ckpts() == [0,10,20,30,40,50,60,70,80,90,98,99]
        for t in [0,10,50]:
            assert ckptr.load(t)['test'] == t

        try:
            ckptr.load(5)
            assert False
        except:
            pass

        try:
            ckptr.save({'test': 1},  1)
            assert False
        except:
            pass

        rmtree('.test_ckpt_dir')

        ckptr = Checkpointer('./.test_ckpt_dir')
        for t in range(100):
            ckptr.save({'test': t},  t)
        for t in range(100):
            assert os.path.exists(ckptr.get_ckpt_path(t))
        rmtree('.test_ckpt_dir')

        ckptr = Checkpointer('./.test_ckpt_dir', min_ckpt_period=10)
        for t in range(100):
            ckptr.save({'test': t},  t)
        for t in range(100):
            assert os.path.exists(ckptr.get_ckpt_path(t))
        rmtree('.test_ckpt_dir')

        ckptr = Checkpointer('./.test_ckpt_dir', max_ckpts_to_keep=3)
        for t in range(100):
            ckptr.save({'test': t},  t)
        assert ckptr.ckpts() == [97,98,99]
        rmtree('.test_ckpt_dir')


if __name__=='__main__':
    unittest.main()

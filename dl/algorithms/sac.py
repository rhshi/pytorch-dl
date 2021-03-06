"""
SAC RL algorithm.
https://arxiv.org/abs/1801.01290
https://arxiv.org/abs/1812.05905
"""
from dl import Trainer
from dl.modules import Policy, QFunction, ValueFunction, TanhDiagGaussian
from dl.util import ReplayBuffer
from dl.util import logger, find_monitor, FrameStack, TBXMonitor
from dl.eval import rl_evaluate, rl_record, rl_plot
import gin, os, time, json
import torch
import torch.nn as nn
import numpy as np


def soft_target_update(target_net, net, tau):
    for tp, p in zip(target_net.parameters(), net.parameters()):
        tp.data.copy_((1. - tau) * tp.data + tau * p.data)

@gin.configurable(blacklist=['logdir'])
class SAC(Trainer):
    def __init__(self,
                 logdir,
                 env_fn,
                 optimizer,
                 policy=Policy,
                 qf=QFunction,
                 vf=ValueFunction,
                 policy_lr=1e-3,
                 qf_lr=1e-3,
                 vf_lr=1e-3,
                 policy_mean_reg_weight=1e-3,
                 policy_std_reg_weight=1e-3,
                 gamma=0.99,
                 batch_size=256,
                 update_period=1,
                 target_update_period=1,
                 policy_update_period=1,
                 frame_stack=1,
                 learning_starts=50000,
                 eval_nepisodes=100,
                 target_smoothing_coef=0.005,
                 automatic_entropy_tuning=True,
                 reparameterization_trick=True,
                 normalize_observations=True,
                 target_entropy=None,
                 reward_scale=1,
                 buffer=ReplayBuffer,
                 buffer_size=1000000,
                 gpu=True,
                 log_period=1000,
                 **trainer_kwargs
    ):
        super().__init__(logdir, **trainer_kwargs)
        tstart = max(self.ckptr.ckpts()) if len(self.ckptr.ckpts()) > 0 else 0
        self.env = TBXMonitor(env_fn(rank=0), tstart=tstart)
        self.env_fn = env_fn
        self.gamma = gamma
        self.batch_size = batch_size
        self.update_period = update_period
        self.target_update_period = target_update_period
        self.policy_update_period = policy_update_period
        self.frame_stack = frame_stack
        self.learning_starts = learning_starts
        self.rsample = reparameterization_trick
        self.reward_scale = reward_scale
        self.target_smoothing_coef = target_smoothing_coef
        self.norm_obs = normalize_observations
        self.eval_nepisodes = eval_nepisodes
        self.log_period = log_period
        self.buffer = buffer(buffer_size, frame_stack)

        s = self.env.observation_space.shape
        ob_shape = (s[0] * self.frame_stack, *s[1:])

        self.discrete = self.env.action_space.__class__.__name__ == 'Discrete'
        if not self.discrete:
            dist = TanhDiagGaussian
        else:
            dist = None
        self.pi  = policy(ob_shape, self.env.action_space,  norm_observations=self.norm_obs, dist=dist)
        self.qf1 = qf(ob_shape, self.env.action_space)
        self.qf2 = qf(ob_shape, self.env.action_space)
        self.vf = vf(ob_shape)
        self.target_vf = vf(ob_shape)

        self.device = torch.device("cuda:0" if gpu and torch.cuda.is_available() else "cpu")
        self.pi.to(self.device)
        self.qf1.to(self.device)
        self.qf2.to(self.device)
        self.vf.to(self.device)
        self.target_vf.to(self.device)

        self.opt_pi = optimizer(self.pi.parameters(), lr=policy_lr)
        self.opt_qf1 = optimizer(self.qf1.parameters(), lr=qf_lr)
        self.opt_qf2 = optimizer(self.qf2.parameters(), lr=qf_lr)
        self.opt_vf = optimizer(self.vf.parameters(), lr=vf_lr)
        self.policy_mean_reg_weight = policy_mean_reg_weight
        self.policy_std_reg_weight = policy_std_reg_weight

        self.target_vf.load_state_dict(self.vf.state_dict())

        self.automatic_entropy_tuning = automatic_entropy_tuning
        if self.automatic_entropy_tuning:
            if target_entropy:
                self.target_entropy = target_entropy
            else:
                self.target_entropy = -np.prod(self.env.action_space.shape).item()  # heuristic value from Tuomas
            self.log_alpha = torch.zeros(1, requires_grad=True, device=self.device)
            self.opt_alpha = optimizer([self.log_alpha], lr=policy_lr)
        else:
            self.target_entropy = None
            self.log_alpha = None
            self.opt_alpha = None

        self.qf_criterion = torch.nn.MSELoss()
        self.vf_criterion = torch.nn.MSELoss()

        self.t, self.t_start = 0,0
        self.losses = {'pi':[], 'vf':[], 'qf1':[], 'qf2':[], 'alpha':[]}

        self._ob = torch.from_numpy(self.env.reset()).to(self.device)

        self._reset()

    def _reset(self):
        self.buffer.env_reset()
        self._ob = self.env.reset()

    def state_dict(self):
        return {
            'pi': self.pi.state_dict(),
            'qf1': self.qf1.state_dict(),
            'qf2': self.qf2.state_dict(),
            'vf': self.vf.state_dict(),
            'opt_pi': self.opt_pi.state_dict(),
            'opt_qf1': self.opt_qf1.state_dict(),
            'opt_qf2': self.opt_qf2.state_dict(),
            'opt_vf': self.opt_vf.state_dict(),
            'log_alpha': self.log_alpha if self.automatic_entropy_tuning else None,
            'opt_alpha': self.opt_alpha.state_dict() if self.automatic_entropy_tuning else None,
            't':   self.t,
            'buffer': self.buffer.state_dict()
        }

    def load_state_dict(self, state_dict):
        self.pi.load_state_dict(state_dict['pi'])
        self.qf1.load_state_dict(state_dict['qf1'])
        self.qf2.load_state_dict(state_dict['qf2'])
        self.vf.load_state_dict(state_dict['vf'])
        self.target_vf.load_state_dict(state_dict['vf'])

        self.opt_pi.load_state_dict(state_dict['opt_pi'])
        self.opt_qf1.load_state_dict(state_dict['opt_qf1'])
        self.opt_qf2.load_state_dict(state_dict['opt_qf2'])
        self.opt_vf.load_state_dict(state_dict['opt_vf'])

        if state_dict['log_alpha']:
            with torch.no_grad():
                self.log_alpha.copy_(state_dict['log_alpha'])
        self.opt_vf.load_state_dict(state_dict['opt_vf'])

        self.buffer.load_state_dict(state_dict['buffer'])
        self.t = state_dict['t']
        self._reset()

    def save(self):
        state = self.state_dict()
        # save buffer seperately and only once (because it is huge)
        buffer_state_dict = state['buffer']
        state_without_buffer = dict(state)
        del state_without_buffer['buffer']
        self.ckptr.save(state_without_buffer, self.t)
        np.savez(os.path.join(self.ckptr.ckptdir, 'buffer.npz'), **buffer_state_dict)

    def load(self, t=None):
        state = self.ckptr.load(t)
        state['buffer'] = np.load(os.path.join(self.ckptr.ckptdir, 'buffer.npz'))
        self.load_state_dict(state)
        self.t_start = self.t

    def act(self):
        idx = self.buffer.store_frame(self._ob)
        x = self.buffer.encode_recent_observation()
        with torch.no_grad():
            x = torch.from_numpy(x).to(self.device)
            ac = self.pi(x[None]).action.cpu().numpy()[0]
        self._ob, r, done, _ = self.env.step(self._unnorm_action(ac))
        self.buffer.store_effect(idx, ac, r, done)
        if done:
            self._ob = self.env.reset()
        self.t += 1
        if self.norm_obs and self.t % 128 == 0:
            idx = self.buffer.next_idx
            if idx >= 128:
                obs = self.buffer.obs[idx-128:idx]
            else:
                obs = np.concatenate([self.buffer.obs[-(128-idx):], self.buffer.obs[:idx]], 0)
            batch_mean = torch.from_numpy(np.mean(obs, axis=0)).to(self.device)
            batch_var  = torch.from_numpy(np.var(obs, axis=0)).to(self.device)
            self.pi.running_norm.update(batch_mean, batch_var, 128)

    def _unnorm_action(self, ac):
        if self.env.action_space.__class__.__name__ == 'Box':
            l = self.env.action_space.low
            h = self.env.action_space.high
            if l is not None and h is not None:
                ac = l + 0.5 * (ac + 1) * (h - l)
        return ac

    def loss(self, batch):
        ob, ac, rew, next_ob, done = [torch.from_numpy(x).to(self.device) for x in batch]

        pi_out = self.pi(ob, reparameterization_trick=self.rsample)
        if self.discrete:
            new_ac = pi_out.action
            logp = pi_out.logp
        else:
            if self.rsample:
                new_ac, new_pth_ac = pi_out.dist.rsample(return_pretanh_value=True)
            else:
                new_ac, new_pth_ac = pi_out.dist.sample(return_pretanh_value=True)
            logp = pi_out.dist.log_prob(new_ac, new_pth_ac)
        if self.norm_obs:
            ob = self.pi.running_norm(ob)
        q1 = self.qf1(ob, ac).value
        q2 = self.qf2(ob, ac).value
        v  = self.vf(ob).value

        # alpha loss
        if self.automatic_entropy_tuning:
            alpha_loss = -(self.log_alpha * (logp + self.target_entropy).detach()).mean()
            self.opt_alpha.zero_grad()
            alpha_loss.backward()
            self.opt_alpha.step()
            alpha = self.log_alpha.exp()
        else:
            alpha = 1
            alpha_loss = 0

        # qf loss
        vtarg = self.target_vf(next_ob).value
        qtarg = self.reward_scale * rew + (1.0 - done) * self.gamma * vtarg
        assert qtarg.shape == q1.shape
        assert qtarg.shape == q2.shape
        qf1_loss = self.qf_criterion(q1, qtarg.detach())
        qf2_loss = self.qf_criterion(q2, qtarg.detach())

        # vf loss
        q1_new = self.qf1(ob, new_ac).value
        q2_new = self.qf2(ob, new_ac).value
        q = torch.min(q1_new, q2_new)
        vtarg = q - alpha * logp
        assert v.shape == vtarg.shape
        vf_loss = self.vf_criterion(v, vtarg.detach())

        # pi loss
        pi_loss = None
        if self.t % self.policy_update_period == 0:
            if self.rsample:
                assert q.shape == logp.shape
                pi_loss = (alpha*logp - q).mean()
            else:
                pi_targ = q - v
                assert pi_targ.shape == logp.shape
                pi_loss = (logp * (alpha * logp - pi_targ).detach()).mean()

            if not self.discrete: # continuous action space.
                pi_loss += self.policy_mean_reg_weight * (pi_out.dist.normal.mean**2).mean()
                pi_loss += self.policy_std_reg_weight * (pi_out.logstd**2).mean()

            self.losses['pi'].append(pi_loss.detach().cpu().numpy())
        self.losses['qf1'].append(qf1_loss.detach().cpu().numpy())
        self.losses['qf2'].append(qf2_loss.detach().cpu().numpy())
        self.losses['vf'].append(vf_loss.detach().cpu().numpy())
        if self.automatic_entropy_tuning:
            self.losses['alpha'].append(alpha_loss.detach().cpu().numpy())
        else:
            self.losses['alpha'].append(alpha_loss)
        if self.t % self.log_period == 0 and self.t > 0:
            if self.automatic_entropy_tuning:
                logger.add_scalar('ent/log_alpha', self.log_alpha.detach().cpu().numpy(), self.t, time.time())
                scalars = {"target": self.target_entropy, "entropy": -torch.mean(logp.detach()).cpu().numpy().item()}
                logger.add_scalars('ent/entropy', scalars, self.t, time.time())
            else:
                logger.add_scalar('ent/entropy', -torch.mean(logp.detach()).cpu().numpy().item(), self.t, time.time())
        return pi_loss, qf1_loss, qf2_loss, vf_loss


    def step(self):
        self.act()
        while self.buffer.num_in_buffer < min(self.learning_starts, self.buffer.size):
            self.act()
        if self.t % self.target_update_period == 0:
            soft_target_update(self.target_vf, self.vf, self.target_smoothing_coef)

        if self.t % self.update_period == 0:
            batch = self.buffer.sample(self.batch_size)

            pi_loss, qf1_loss, qf2_loss, vf_loss = self.loss(batch)

            # update
            self.opt_qf1.zero_grad()
            qf1_loss.backward()
            self.opt_qf1.step()

            self.opt_qf2.zero_grad()
            qf2_loss.backward()
            self.opt_qf2.step()

            self.opt_vf.zero_grad()
            vf_loss.backward()
            self.opt_vf.step()

            if pi_loss:
                self.opt_pi.zero_grad()
                pi_loss.backward()
                self.opt_pi.step()

        if self.t % self.log_period == 0 and self.t > 0:
            self.log()


    def log(self):
        logger.log("========================|  Timestep: {}  |========================".format(self.t))
        for k,v in self.losses.items():
            logger.logkv(f'Loss - {k}', np.mean(v))
            logger.add_scalar(f'loss/{k}', np.mean(v), self.t, time.time())
            self.losses[k] = []
        # Logging stats...
        logger.logkv('timesteps', self.t)
        logger.logkv('fps', int((self.t - self.t_start) / (time.monotonic() - self.time_start)))
        logger.logkv('time_elapsed', time.monotonic() - self.time_start)

        monitor = find_monitor(self.env)
        if monitor is not None:
            logger.logkv('mean episode length', np.mean(monitor.episode_lengths[-100:]))
            logger.logkv('mean episode reward', np.mean(monitor.episode_rewards[-100:]))
        logger.dumpkvs()



    def evaluate(self):
        self.pi.train(False)
        eval_env = self.env_fn(rank=1)
        if self.frame_stack > 1:
            eval_env = FrameStack(eval_env, self.frame_stack)

        os.makedirs(os.path.join(self.logdir, 'eval'), exist_ok=True)
        outfile = os.path.join(self.logdir, 'eval', self.ckptr.format.format(self.t) + '.json')
        stats = rl_evaluate(eval_env, self.pi, self.eval_nepisodes, outfile, self.device)
        logger.add_scalar('eval/mean_episode_reward', stats['mean_reward'], self.t, time.time())
        logger.add_scalar('eval/mean_episode_length', stats['mean_length'], self.t, time.time())

        os.makedirs(os.path.join(self.logdir, 'video'), exist_ok=True)
        outfile = os.path.join(self.logdir, 'video', self.ckptr.format.format(self.t) + '.mp4')
        rl_record(eval_env, self.pi, 5, outfile, self.device)

        if find_monitor(self.env):
            rl_plot(os.path.join(self.logdir, 'logs'), self.env.spec.id, self.t)
        self.pi.train(True)


    def close(self):
        if hasattr(self.env, 'close'):
            self.env.close()
        logger.reset()




import unittest, shutil, gym
from dl.util import atari_env, load_gin_configs, Monitor

class TestSAC(unittest.TestCase):
    def test_sac(self):
        sac = SAC('logs', learning_starts=300, eval_nepisodes=1, buffer_size=500, target_update_period=100, maxt=1000, eval=False, eval_period=1000, reparameterization_trick=False)
        sac.train()
        sac = SAC('logs', learning_starts=300, eval_nepisodes=1, buffer_size=500, maxt=1000, eval=False, eval_period=1000, reparameterization_trick=False)
        sac.train() # loads checkpoint
        assert sac.buffer.num_in_buffer == 500
        shutil.rmtree('logs')


if __name__=='__main__':
    load_gin_configs(['../configs/sac.gin'])
    unittest.main()

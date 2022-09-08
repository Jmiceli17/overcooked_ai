from arguments import get_arguments
from agent import OAIAgent, OAITrainer
from networks import GridEncoder, MLP, weights_init_, get_output_shape
from overcooked_ai_py.mdp.overcooked_mdp import Action
from overcooked_ai_py.visualization.state_visualizer import StateVisualizer
from overcooked_dataset import OvercookedDataset, Subtasks
from overcooked_gym_env import OvercookedGymEnv
from state_encodings import ENCODING_SCHEMES

from copy import deepcopy
import numpy as np
from pathlib import Path
import pygame
from pygame.locals import HWSURFACE, DOUBLEBUF, RESIZABLE, QUIT, VIDEORESIZE
from tqdm import tqdm
import time
import torch as th
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.distributions.categorical import Categorical
from typing import Dict, Any
import wandb


class BehaviouralCloningPolicy(nn.Module):
    def __init__(self, visual_obs_shape, agent_obs_shape, args, act=nn.ReLU, hidden_dim=256):
        """
        NN network for a behavioral cloning agent
        :param visual_obs_shape: Shape of any grid-like input to be passed into a CNN
        :param agent_obs_shape: Shape of any vector input to passed only into an MLP
        :param depth: Depth of CNN
        :param act: activation function
        :param hidden_dim: hidden dimension to use in NNs
        """
        super(BehaviouralCloningPolicy, self).__init__()
        self.device = args.device
        # NOTE The policy only uses subtasks as an input. Policies only output actions
        self.use_subtasks = args.use_subtasks
        self.use_visual_obs = np.prod(visual_obs_shape) > 0
        assert len(agent_obs_shape) == 1
        self.use_agent_obs = np.prod(agent_obs_shape) > 0
        self.subtasks_obs_size = Subtasks.NUM_SUBTASKS if self.use_subtasks else 0

        # Define CNN for grid-like observations
        if self.use_visual_obs:
            self.cnn = GridEncoder(visual_obs_shape)
            self.cnn_output_shape = get_output_shape(self.cnn, [1, *visual_obs_shape])[0]
        else:
            self.cnn_output_shape = 0

        # Define MLP for vector/feature based observations
        self.mlp = MLP(input_dim=self.cnn_output_shape + agent_obs_shape[0] + self.subtasks_obs_size,
                       output_dim=hidden_dim, hidden_dim=hidden_dim, act=act)
        self.action_predictor = nn.Linear(hidden_dim, Action.NUM_ACTIONS)

        self.apply(weights_init_)
        self.to(self.device)

    def get_latent_feats(self, obs):
        mlp_input = []
        # Concatenate all input features before passing them to MLP
        if self.use_visual_obs:
            mlp_input.append(self.cnn.forward(obs['visual_obs']))
        if self.use_agent_obs:
            mlp_input.append(obs['agent_obs'])
        if self.use_subtasks:
            mlp_input.append(obs['subtask'])
        return self.mlp.forward(th.cat(mlp_input, dim=-1))

    def forward(self, obs):
        return self.action_predictor(self.get_latent_feats(obs))

    def predict(self, obs, sample=True):
        """Predict action. If sample is True, sample action from distribution, else pick best scoring action"""
        return Categorical(logits=self.forward(obs)).sample() if sample else th.argmax(self.forward(obs), dim=-1), None

    def get_distribution(self, obs):
        return Categorical(logits=self.forward(obs))


class BehaviouralCloningAgent(OAIAgent):
    def __init__(self, visual_obs_shape, agent_obs_shape, p_idx, args, hidden_dim=256, name=None):
        name = name or f'il_p{p_idx + 1}'
        super(BehaviouralCloningAgent, self).__init__(name, p_idx, args)
        self.visual_obs_shape, self.agent_obs_shape, self.args, self.hidden_dim = \
             visual_obs_shape, agent_obs_shape, args, hidden_dim
        self.device = args.device
        self.use_subtasks = args.use_subtasks
        self.policy = BehaviouralCloningPolicy(visual_obs_shape, agent_obs_shape, args, hidden_dim=hidden_dim)
        if self.use_subtasks:
            self.subtask_predictor = nn.Linear(hidden_dim, Subtasks.NUM_SUBTASKS)
            self.apply(weights_init_)
        self.to(self.device)

    def _get_constructor_parameters(self) -> Dict[str, Any]:
        """
        Get data that need to be saved in order to re-create the model when loading it from disk.
        :return: The dictionary to pass to the as kwargs constructor when reconstruction this model.
        """
        return dict(
            visual_obs_shape=self.visual_obs_shape,
            agent_obs_shape=self.agent_obs_shape,
            p_idx=self.p_idx,
            args = self.args,
            hidden_dim = self.hidden_dim
        )

    def forward(self, obs):
        z = self.policy.get_latent_feats(obs)
        action_logits = self.policy.action_predictor(z)
        return (action_logits, self.subtask_predictor(z)) if self.use_subtasks else action_logits

    def predict(self, obs, sample=True):
        obs = {k: th.tensor(v, device=self.device).unsqueeze(0) for k, v in obs.items()}
        if self.use_subtasks:
            obs['subtask'] = self.curr_subtask.unsqueeze(0)
        logits = self.forward(obs)
        action_logits = logits[0] if self.use_subtasks else logits
        action = Categorical(logits=action_logits).sample() if sample else th.argmax(action_logits, dim=-1)
        if self.use_subtasks:
            _, subtask_logits = logits
            # Update predicted subtask
            if Action.INDEX_TO_ACTION[action] == Action.INTERACT or self.first_step:
                ps = th.zeros_like(subtask_logits.squeeze())
                subtask_id = th.argmax(subtask_logits.detach().squeeze(), dim=-1)
                ps[subtask_id] = 1
                self.curr_subtask = ps.float()
                self.first_step = False
                print('new subtask', Subtasks.IDS_TO_SUBTASKS[subtask_id.item()])
        return action, None

    def get_distribution(self, obs: th.Tensor):
        obs = {k: th.tensor(v, device=self.device).unsqueeze(0) for k, v in obs.items()}
        if self.use_subtasks:
            obs['subtask'] = self.curr_subtask.unsqueeze(0)
        return self.policy.get_distribution(obs)

    def reset(self, state):
        # Predicted subtask to perform next, stars as unknown
        unknown_task_id = th.tensor(Subtasks.SUBTASKS_TO_IDS['unknown']).to(self.device)
        self.curr_subtask = F.one_hot(unknown_task_id, num_classes=Subtasks.NUM_SUBTASKS)
        self.first_step = True


class BehavioralCloningTrainer(OAITrainer):
    def __init__(self, dataset, args, vis_eval=False):
        """
        Class to train BC agent
        :param env: Overcooked environment to use
        :param dataset: That dataset to train on - can be None if the only visualizing agetns
        :param args: arguments to use
        :param vis_eval: If true, the evaluate function will visualize the agents
        """
        super(BehavioralCloningTrainer, self).__init__('bc', args)
        self.device = th.device('cuda' if th.cuda.is_available() else 'cpu')
        self.num_players = 2
        self.dataset = dataset
        self.use_subtasks = args.use_subtasks
        self.train_dataset = OvercookedDataset(dataset, [args.layout_name], args)
        self.grid_shape = self.train_dataset.grid_shape
        self.eval_env = OvercookedGymEnv(grid_shape=self.grid_shape, args=args)
        obs = self.eval_env.get_obs()
        visual_obs_shape = obs['visual_obs'][0].shape
        agent_obs_shape = obs['agent_obs'][0].shape
        self.agents = (
            BehaviouralCloningAgent(visual_obs_shape, agent_obs_shape, 0, args),
            BehaviouralCloningAgent(visual_obs_shape, agent_obs_shape, 1, args)
        )
        self.optimizers = tuple([th.optim.Adam(agent.parameters(), lr=args.lr) for agent in self.agents])
        action_weights = th.tensor(self.train_dataset.get_action_weights(), dtype=th.float32, device=self.device)
        self.action_criterion = nn.CrossEntropyLoss(weight=action_weights)
        if self.use_subtasks:
            subtask_weights = th.tensor(self.train_dataset.get_subtask_weights(), dtype=th.float32,
                                        device=self.device)
            self.subtask_criterion = nn.CrossEntropyLoss(weight=subtask_weights, reduction='none')

        if vis_eval:
            self.eval_env.setup_visualization()

    def train_on_batch(self, batch):
        """Train BC agent on a batch of data"""
        # print({k: v for k,v in batch.items()})
        batch = {k: v.to(self.device) for k, v in batch.items()}
        action, subtasks = batch['joint_action'].long(), batch['subtasks'].long()
        curr_subtask, next_subtask = subtasks[:, 0], subtasks[:, 1]
        metrics = {}
        for i in range(self.num_players):
            self.optimizers[i].zero_grad()
            cs_i = F.one_hot(curr_subtask[:, i], num_classes=Subtasks.NUM_SUBTASKS)
            obs = {k: batch[k][:, i] for k in ['visual_obs', 'agent_obs']}
            obs['subtask'] = cs_i
            preds = self.agents[i].forward(obs)
            tot_loss = 0
            if self.use_subtasks:
                # Train on subtask prediction task
                pred_action, pred_subtask = preds
                subtask_loss = self.subtask_criterion(pred_subtask, next_subtask[:, i])

                subtask_mask = action[:, i] == Action.ACTION_TO_INDEX[Action.INTERACT]
                loss_mask = subtask_mask #th.logical_or(subtask_mask, th.rand_like(subtask_loss, device=self.device) > 0.95)
                subtask_loss = th.mean(subtask_loss * loss_mask)
                tot_loss += th.mean(subtask_loss)
                metrics[f'p{i}_subtask_loss'] = subtask_loss.item()
                pred_subtask_indices = th.argmax(pred_subtask, dim=-1)
                accuracy = ((pred_subtask_indices == next_subtask[:, i]).float() * subtask_mask).sum() / \
                           subtask_mask.float().sum()
                metrics[f'p{i}_subtask_acc'] = accuracy.item()
            else:
                pred_action = preds
            # Train on action prediction task
            action_loss = self.action_criterion(pred_action, action[:, i])
            metrics[f'p{i}_action_loss'] = action_loss.item()
            tot_loss += action_loss

            tot_loss.backward()
            self.optimizers[i].step()
        return metrics

    def train_epoch(self):
        metrics = {}
        for i in range(2):
            self.agents[i].train()

        count = 0
        dataloader = DataLoader(self.train_dataset, batch_size=self.args.batch_size, shuffle=True, num_workers=4)
        for batch in tqdm(dataloader):
            new_losses = self.train_on_batch(batch)
            metrics = {k: [new_losses[k]] + metrics.get(k, []) for k in new_losses}
            count += 1

        metrics = {k: np.mean(v) for k, v in metrics.items()}
        metrics['total_loss'] = sum([v for k, v in metrics.items() if 'loss' in k])
        return metrics

    def train_agents(self, epochs=100, exp_name=None):
        """ Training routine """
        exp_name = exp_name or self.args.exp_name
        run = wandb.init(project="overcooked_ai_test", entity=self.args.wandb_ent, dir=str(self.args.base_dir / 'wandb'),
                         reinit=True, name='_'.join([exp_name, self.args.layout_name, 'bc']),
                         mode=self.args.wandb_mode)

        for i in range(2):
            self.agents[i].policy.train()
        best_path, best_tag = None, None
        best_reward = 0
        for epoch in range(epochs):
            metrics = self.train_epoch()
            if (epoch + 1) % 10 == 0:
                mean_reward, shaped_reward = self.evaluate()
                wandb.log({'eval_true_reward': mean_reward, 'eval_shaped_reward': shaped_reward, 'epoch': epoch, **metrics})
                if mean_reward > best_reward:
                    print(f'Best reward achieved on epoch {epoch}, saving models')
                    best_path, best_tag = self.save(tag='best_reward')
                    best_reward = mean_reward
        if best_path is not None:
            self.load(best_path, best_tag)
        run.finish()

    def evaluate(self, num_trials=1, sample=True):
        """
        Evaluate agent on <num_trials> trials. Returns average true reward and average shaped reward trials.
        :param num_trials: Number of trials to run
        :param sample: Boolean. If true sample from action distribution. If false, always take 'best' action.
                       NOTE: if sample is false, there is no point in running more than a single trial since the system
                             becomes deterministic
        :return: average true reward and average shaped reward
        """
        average_reward = []
        shaped_reward = []
        for trial in range(num_trials):
            self.eval_env.reset()
            for i, p in enumerate(self.agents):
                p.reset(self.eval_env.state, i)
            trial_reward, trial_shaped_r = 0, 0
            done = False
            timestep = 0
            while not done:
                # Encode Overcooked state into observations for agents
                obs = self.eval_env.get_obs()
                # Get next actions - we don't use overcooked gym env for this because we want to allow subtasks
                joint_action = []
                for i in range(2):
                    agent_obs = {k: v[i] for k, v in obs.items()}
                    action, _ = self.agents[i].predict(agent_obs, sample)
                    joint_action.append(action)
                joint_action = tuple(joint_action)
                # Environment step
                next_state, reward, done, info = self.eval_env.step(joint_action)
                # Update metrics
                trial_reward += np.sum(info['sparse_r_by_agent'])
                trial_shaped_r += np.sum(info['shaped_r_by_agent'])
                timestep += 1
                if (timestep+1) % 200 == 0:
                    print(f'Reward of {trial_reward} at step {timestep}')
            average_reward.append(trial_reward)
            shaped_reward.append(trial_shaped_r)
        return np.mean(average_reward), np.mean(shaped_reward)

    def get_agent(self, p_idx):
        return self.agents[p_idx]



if __name__ == '__main__':
    args = get_arguments()
    eval_only = False
    if eval_only:
        bct = BehavioralCloningTrainer('tf_test_5_5.2.pickle', args, vis_eval=True)
        bct.load()
        bct.evaluate(10)
    else:
        args.use_subtasks = True
        args.batch_size = 4
        args.layout_name = 'tf_test_5_5'
        bct = BehavioralCloningTrainer('tf_test_5_5.2.pickle', args, vis_eval=True)
        # bct.train_agents()
        # bct = BehavioralCloningTrainer(encoding_fn, 'all', 'asymmetric_advantages', args, vis_eval=False)
        # bct.training('all')
        # del bct
        #
        # bct = BehavioralCloningTrainer(encoding_fn, 'all', 'asymmetric_advantages', args, vis_eval=False, use_subtasks=False)
        # bct.training('all_no_subtask')
        # del bct

        # bct = BehavioralCloningTrainer(encoding_fn, ['asymmetric_advantages'], 'asymmetric_advantages', args, vis_eval=False)
        # bct.train_agents('single')
        # del bct

        # bct = BehavioralCloningTrainer(encoding_fn, ['asymmetric_advantages'], 'asymmetric_advantages', args, vis_eval=False, use_subtasks=False)
        # bct.training('single_no_subtask')
        # del bct
        #
        # bct = BehavioralCloningTrainer(encoding_fn, ['cramped_room','coordination_ring','counter_circuit','forced_coordination'], 'asymmetric_advantages', args, vis_eval=False)
        # bct.training('all_but')
        # del bct
        #
        # bct = BehavioralCloningTrainer(encoding_fn, ['cramped_room','coordination_ring','counter_circuit','forced_coordination'], 'asymmetric_advantages', args, vis_eval=False, use_subtasks=False)
        # bct.training('all_but_no_subtask')
        # del bct

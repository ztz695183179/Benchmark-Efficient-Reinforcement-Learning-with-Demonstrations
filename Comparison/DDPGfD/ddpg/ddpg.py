import os
import time
from collections import deque
import pickle
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')

from ddpg.ddpg_learner import DDPG
from ddpg.models import Actor, Critic
from ddpg.memory import Memory
from ddpg.noise import AdaptiveParamNoiseSpec, NormalActionNoise, OrnsteinUhlenbeckActionNoise

import common.tf_util as U

import logger
import numpy as np
from mpi4py import MPI


def learn(save_path, network, env,
          seed=None,
          total_timesteps=None,
          nb_epochs=None, # with default settings, perform 1M steps total
          nb_epoch_cycles=7, # 50
          nb_rollout_steps=3,  #100
          reward_scale=1.0,
          render=False,
          render_eval=False,
        #   noise_type='adaptive-param_0.2',
        #   noise_type='normal_0.8',
          noise_type='normal_2.0',

        #   noise_type='ou_0.9',

          normalize_returns=False,
          normalize_observations=True,
          critic_l2_reg=1e-2,
          actor_lr=1e-4,
          critic_lr=1e-3,
        #   actor_lr=1e-6,
        #   critic_lr=1e-5,
          popart=False,
          gamma=0.99,
          clip_norm=None,
          nb_train_steps=3, # per epoch cycle and MPI worker,  50
          nb_eval_steps=1,  #100
          batch_size=640, # per MPI worker
          tau=0.01,
          eval_env=None,
          param_noise_adaption_interval=3, #50
          **network_kwargs):


    if total_timesteps is not None:
        assert nb_epochs is None
        nb_epochs = int(total_timesteps) // (nb_epoch_cycles * nb_rollout_steps)
    else:
        nb_epochs = 500

    rank = MPI.COMM_WORLD.Get_rank()
    nb_actions = env.num_actions

    action_shape=np.array(nb_actions*[0]).shape

    # nb_features = 2*(env.num_actions+1)+env.num_actions  # with link length, 11 dim
    # nb_features = 2*(env.num_actions+1)  # no link length, 8 dim
    nb_features = 2*(env.num_actions+2)  # no link length, with target position, 10 dim

    observation_shape=np.array(nb_features*[0]).shape
    # assert (np.abs(env.action_space.low) == env.action_space.high).all()  # we assume symmetric actions.

    memory = Memory(limit=int(1e6), action_shape=action_shape, observation_shape=observation_shape)
    demon_buffer = Memory(limit=int(1e6), action_shape=action_shape, observation_shape=observation_shape)

    critic = Critic(network=network, **network_kwargs)
    actor = Actor(nb_actions, network=network, **network_kwargs)

    action_noise = None
    param_noise = None
    # nb_actions = env.action_space.shape[-1]
    if noise_type is not None:
        for current_noise_type in noise_type.split(','):
            current_noise_type = current_noise_type.strip()
            if current_noise_type == 'none':
                pass
            elif 'adaptive-param' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                param_noise = AdaptiveParamNoiseSpec(initial_stddev=float(stddev), desired_action_stddev=float(stddev))
            elif 'normal' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                action_noise = NormalActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
            elif 'ou' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                action_noise = OrnsteinUhlenbeckActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
            else:
                raise RuntimeError('unknown noise type "{}"'.format(current_noise_type))

    # max_action = env.action_space.high
    # logger.info('scaling actions by {} before executing in env'.format(max_action))

    agent = DDPG(actor, critic, memory, demon_buffer, observation_shape, action_shape,
        gamma=gamma, tau=tau, normalize_returns=normalize_returns, normalize_observations=normalize_observations,
        batch_size=batch_size, action_noise=action_noise, param_noise=param_noise, critic_l2_reg=critic_l2_reg,
        actor_lr=actor_lr, critic_lr=critic_lr, enable_popart=popart, clip_norm=clip_norm,
        reward_scale=reward_scale)
    logger.info('Using agent with the following configuration:')
    logger.info(str(agent.__dict__.items()))

    eval_episode_rewards_history = deque(maxlen=100)
    episode_rewards_history = deque(maxlen=100)
    sess = U.get_session()
    # Prepare everything.
    agent.initialize(sess)
    # sess.graph.finalize()

    agent.reset()

    obs = env.reset()
    if eval_env is not None:
        eval_obs = eval_env.reset()
    nenvs = obs.shape[0]

    episode_reward = np.zeros(nenvs, dtype = np.float32) #vector
    episode_step = np.zeros(nenvs, dtype = int) # vector
    episodes = 0 #scalar
    t = 0 # scalar
    step_set=[]
    reward_set=[]

    epoch = 0

    start_time = time.time()

    epoch_episode_rewards = []
    mean_epoch_episode_rewards = []
    epoch_episode_steps = []
    epoch_actions = []
    epoch_qs = []
    episode_end_distance = []
    epoch_episodes = 0
    ''' feeding the separate demonstration buffer with demonstration dataset '''
    agent.feed_demon_buffer()
    SPARSE_REWARD = True
    for epoch in range(nb_epochs):
        print(nb_epochs)
        # obs, env_state = env.reset()
        obs = env.reset()
        agent.save(save_path)
        epoch_episode_rewards=[]
        ''' feed memory with demonstrations'''
        # agent.feed_demon2memory()
        # if epoch < 50:
        #     agent.feed_demon2memory()

        # if epoch % 100 ==0:
        #     agent.feed_demon2memory()



        for cycle in range(nb_epoch_cycles):

            for t_rollout in range(nb_rollout_steps):
                # Predict next action.
                action, q, _, _ = agent.step(obs, apply_noise=True, compute_Q=True)
                if SPARSE_REWARD:
                    new_obs, r, done, end_distance = env.step(action, SPARSE_REWARD)
                else: 
                    new_obs, r, done = env.step(action, SPARSE_REWARD)
                t += 1

                episode_reward += r
                episode_step += 1

                # Book-keeping.
                epoch_actions.append(action)
                epoch_qs.append(q)
                b=1.
                # if epoch <4:
                agent.store_transition(obs, action, r, new_obs, done) #the batched data will be unrolled in memory.py's append.
                obs = new_obs

            epoch_episode_rewards.append(episode_reward)

            episode_reward = np.zeros(nenvs, dtype = np.float32) #vector

            if cycle == nb_epoch_cycles-1:
                # record the distance from the end position of reacher to the goal for the last step of each episode
                if SPARSE_REWARD:
                    episode_end_distance.append(end_distance)
                else:
                    end_distance = 100.0/r-1
                    episode_end_distance.append(end_distance[0])

            # Train.
            epoch_actor_losses = []
            epoch_critic_losses = []
            epoch_adaptive_distances = []
            for t_train in range(nb_train_steps):
                # Adapt param noise, if necessary.
                if memory.nb_entries >= batch_size and t_train % param_noise_adaption_interval == 0:
                    distance = agent.adapt_param_noise()
                    epoch_adaptive_distances.append(distance)
                cl, al = agent.train()
                epoch_critic_losses.append(cl)
                epoch_actor_losses.append(al)
                agent.update_target_net()

            # Evaluate.
            eval_episode_rewards = []
            eval_qs = []
            if eval_env is not None:
                nenvs_eval = eval_obs.shape[0]
                eval_episode_reward = np.zeros(nenvs_eval, dtype = np.float32)
                for t_rollout in range(nb_eval_steps):
                    eval_action, eval_q, _, _ = agent.step(eval_obs, apply_noise=False, compute_Q=True)
                    # eval_obs, eval_r, eval_done, eval_info = eval_env.step(max_action * eval_action)  # scale for execution in env (as far as DDPG is concerned, every action is in [-1, 1])
                    eval_obs, eval_r, eval_done, eval_info = eval_env.step( eval_action)
                    if render_eval:
                        eval_env.render()
                    eval_episode_reward += eval_r

                    eval_qs.append(eval_q)
                    for d in range(len(eval_done)):
                        if eval_done[d]:
                            eval_episode_rewards.append(eval_episode_reward[d])
                            eval_episode_rewards_history.append(eval_episode_reward[d])
                            eval_episode_reward[d] = 0.0

        mpi_size = MPI.COMM_WORLD.Get_size()
        # Log stats.
        # XXX shouldn't call np.mean on variable length lists
        duration = time.time() - start_time
        stats = agent.get_stats()
        combined_stats = stats.copy()
        combined_stats['rollout/return'] = np.mean(epoch_episode_rewards)
        combined_stats['rollout/return_history'] = np.mean(episode_rewards_history)
        combined_stats['rollout/episode_steps'] = np.mean(epoch_episode_steps)
        combined_stats['rollout/actions_mean'] = np.mean(epoch_actions)
        combined_stats['rollout/Q_mean'] = np.mean(epoch_qs)
        combined_stats['train/loss_actor'] = np.mean(epoch_actor_losses)
        combined_stats['train/loss_critic'] = np.mean(epoch_critic_losses)
        combined_stats['train/param_noise_distance'] = np.mean(epoch_adaptive_distances)
        combined_stats['total/duration'] = duration
        combined_stats['total/steps_per_second'] = float(t) / float(duration)
        combined_stats['total/episodes'] = episodes
        combined_stats['rollout/episodes'] = epoch_episodes
        combined_stats['rollout/actions_std'] = np.std(epoch_actions)

        mean_epoch_episode_rewards.append(np.mean(epoch_episode_rewards))
        # print(step_set,mean_epoch_episode_rewards)
        step_set.append(t)
        plt.figure(1)
        plt.plot(step_set,mean_epoch_episode_rewards)
        plt.xlabel('Steps')
        plt.ylabel('Mean Episode Reward')
        plt.savefig('ddpg_mean.png')

        plt.figure(2)
        plt.plot(step_set, episode_end_distance)
        plt.xlabel('Steps')
        plt.ylabel('Distance to Target')
        plt.savefig('ddpgfd_distance.png')
        # plt.show()

        # Evaluation statistics.
        if eval_env is not None:
            combined_stats['eval/return'] = eval_episode_rewards
            combined_stats['eval/return_history'] = np.mean(eval_episode_rewards_history)
            combined_stats['eval/Q'] = eval_qs
            combined_stats['eval/episodes'] = len(eval_episode_rewards)
        def as_scalar(x):
            if isinstance(x, np.ndarray):
                assert x.size == 1
                return x[0]
            elif np.isscalar(x):
                return x
            else:
                raise ValueError('expected scalar, got %s'%x)

        combined_stats_sums = MPI.COMM_WORLD.allreduce(np.array([ np.array(x).flatten()[0] for x in combined_stats.values()]))
        combined_stats = {k : v / mpi_size for (k,v) in zip(combined_stats.keys(), combined_stats_sums)}

        # Total statistics.
        combined_stats['total/epochs'] = epoch + 1
        combined_stats['total/steps'] = t

        for key in sorted(combined_stats.keys()):
            logger.record_tabular(key, combined_stats[key])

        if rank == 0:
            logger.dump_tabular()
        logger.info('')
        logdir = logger.get_dir()
        if rank == 0 and logdir:
            if hasattr(env, 'get_state'):
                with open(os.path.join(logdir, 'env_state.pkl'), 'wb') as f:
                    pickle.dump(env.get_state(), f)
            if eval_env and hasattr(eval_env, 'get_state'):
                with open(os.path.join(logdir, 'eval_env_state.pkl'), 'wb') as f:
                    pickle.dump(eval_env.get_state(), f)

    print('stepset: ',step_set)
    print('rewards: ',mean_epoch_episode_rewards)
    print('distances: ', episode_end_distance)
    return agent



# test not modify yet
def testing(save_path, network, env,
          seed=None,
          total_timesteps=None,
          nb_epochs=None, # with default settings, perform 1M steps total
          nb_epoch_cycles=7, # 50
          nb_rollout_steps=3,  #100
          reward_scale=1.0,
          render=False,
          render_eval=False,
        #   noise_type='adaptive-param_0.2',
        #   noise_type='normal_0.8',
          noise_type='normal_5.0',

        #   noise_type='ou_0.9',

          normalize_returns=False,
          normalize_observations=True,
          critic_l2_reg=1e-2,
          actor_lr=1e-4,
          critic_lr=1e-3,
        #   actor_lr=1e-6,
        #   critic_lr=1e-5,
          popart=False,
          gamma=0.99,
          clip_norm=None,
          nb_train_steps=3, # per epoch cycle and MPI worker,  50
          nb_eval_steps=1,  #100
          batch_size=640, # per MPI worker
          tau=0.01,
          eval_env=None,
          param_noise_adaption_interval=3, #50
          **network_kwargs):



    if total_timesteps is not None:
        assert nb_epochs is None
        nb_epochs = int(total_timesteps) // (nb_epoch_cycles * nb_rollout_steps)
    else:
        nb_epochs = 500

    rank = MPI.COMM_WORLD.Get_rank()
    nb_actions = env.num_actions

    action_shape=np.array(nb_actions*[0]).shape

    # nb_features = 2*(env.num_actions+1)+env.num_actions  # with link length, 11 dim
    # nb_features = 2*(env.num_actions+1)  # no link length, 8 dim
    nb_features = 2*(env.num_actions+2)  # no link length, with target position, 10 dim

    observation_shape=np.array(nb_features*[0]).shape
    # memory = Memory(limit=int(1e6), action_shape=env.action_space.shape, observation_shape=env.observation_space.shape)
    memory = Memory(limit=int(1e6), action_shape=action_shape, observation_shape=observation_shape)
    critic = Critic(network=network, **network_kwargs)
    actor = Actor(nb_actions, network=network, **network_kwargs)

    action_noise = None
    param_noise = None

    '''no noise for test'''
    # if noise_type is not None:
    #     for current_noise_type in noise_type.split(','):
    #         current_noise_type = current_noise_type.strip()
    #         if current_noise_type == 'none':
    #             pass
    #         elif 'adaptive-param' in current_noise_type:
    #             _, stddev = current_noise_type.split('_')
    #             param_noise = AdaptiveParamNoiseSpec(initial_stddev=float(stddev), desired_action_stddev=float(stddev))
    #         elif 'normal' in current_noise_type:
    #             _, stddev = current_noise_type.split('_')
    #             action_noise = NormalActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
    #         elif 'ou' in current_noise_type:
    #             _, stddev = current_noise_type.split('_')
    #             action_noise = OrnsteinUhlenbeckActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
    #         else:
    #             raise RuntimeError('unknown noise type "{}"'.format(current_noise_type))

    # max_action = env.action_space.high
    # logger.info('scaling actions by {} before executing in env'.format(max_action))

    # agent = DDPG(actor, critic, memory, env.observation_space.shape, env.action_space.shape,
    agent = DDPG(actor, critic, memory, observation_shape, action_shape,
        gamma=gamma, tau=tau, normalize_returns=normalize_returns, normalize_observations=normalize_observations,
        batch_size=batch_size, action_noise=action_noise, param_noise=param_noise, critic_l2_reg=critic_l2_reg,
        actor_lr=actor_lr, critic_lr=critic_lr, enable_popart=popart, clip_norm=clip_norm,
        reward_scale=reward_scale)
    logger.info('Using agent with the following configuration:')
    logger.info(str(agent.__dict__.items()))

    eval_episode_rewards_history = deque(maxlen=100)
    episode_rewards_history = deque(maxlen=100)
    sess = U.get_session()
    # Prepare everything.
    agent.load(sess,save_path)
    # sess.graph.finalize()  # cannot save sess if its finalized!

    agent.reset()

    obs = env.reset()
    if eval_env is not None:
        eval_obs = eval_env.reset()
    nenvs = obs.shape[0]

    episode_reward = np.zeros(nenvs, dtype = np.float32) #vector
    episode_step = np.zeros(nenvs, dtype = int) # vector
    episodes = 0 #scalar
    t = 0 # scalar
    step_set=[]
    reward_set=[]

    epoch = 0



    start_time = time.time()

    epoch_episode_rewards = []
    mean_epoch_episode_rewards = []
    epoch_episode_steps = []
    epoch_actions = []
    epoch_qs = []
    epoch_episodes = 0
    SPARSE_REWARD=False
    for epoch in range(nb_epochs):
        print(nb_epochs)
        obs = env.reset()
        epoch_episode_rewards=[]
        for cycle in range(nb_epoch_cycles):
            # Perform rollouts.

            for t_rollout in range(nb_rollout_steps):
                # Predict next action.
                '''no noise for test'''
                action, q, _, _ = agent.step(obs, apply_noise=False, compute_Q=True)

                if SPARSE_REWARD:
                    new_obs, r, done, end_distance = env.step(action, SPARSE_REWARD)
                else: 
                    new_obs, r, done = env.step(action, SPARSE_REWARD)

                t += 1

                episode_reward += r
                episode_step += 1

                # Book-keeping.
                epoch_actions.append(action)
                epoch_qs.append(q)
                b=1.
                agent.store_transition(obs, action, r, new_obs, done) #the batched data will be unrolled in memory.py's append.

                obs = new_obs


            epoch_episode_rewards.append(episode_reward)

            episode_reward = np.zeros(nenvs, dtype = np.float32) #vector

            # Train.
            epoch_actor_losses = []
            epoch_critic_losses = []
            epoch_adaptive_distances = []
            '''no training for test'''
            # for t_train in range(nb_train_steps):
                # Adapt param noise, if necessary. no noise for test!
                # if memory.nb_entries >= batch_size and t_train % param_noise_adaption_interval == 0:
                #     distance = agent.adapt_param_noise()
                #     epoch_adaptive_distances.append(distance)

                # cl, al = agent.train()
                # epoch_critic_losses.append(cl)
                # epoch_actor_losses.append(al)
                # agent.update_target_net()

            # Evaluate.
            eval_episode_rewards = []
            eval_qs = []
            if eval_env is not None:
                nenvs_eval = eval_obs.shape[0]
                eval_episode_reward = np.zeros(nenvs_eval, dtype = np.float32)
                for t_rollout in range(nb_eval_steps):
                    eval_action, eval_q, _, _ = agent.step(eval_obs, apply_noise=False, compute_Q=True)
                    # eval_obs, eval_r, eval_done, eval_info = eval_env.step(max_action * eval_action)  # scale for execution in env (as far as DDPG is concerned, every action is in [-1, 1])
                    eval_obs, eval_r, eval_done, eval_info = eval_env.step( eval_action)
                    if render_eval:
                        eval_env.render()
                    eval_episode_reward += eval_r

                    eval_qs.append(eval_q)
                    for d in range(len(eval_done)):
                        if eval_done[d]:
                            eval_episode_rewards.append(eval_episode_reward[d])
                            eval_episode_rewards_history.append(eval_episode_reward[d])
                            eval_episode_reward[d] = 0.0

        mpi_size = MPI.COMM_WORLD.Get_size()
        # Log stats.
        # XXX shouldn't call np.mean on variable length lists
        duration = time.time() - start_time
        stats = agent.get_stats()
        combined_stats = stats.copy()
        combined_stats['rollout/return'] = np.mean(epoch_episode_rewards)
        combined_stats['rollout/return_history'] = np.mean(episode_rewards_history)
        combined_stats['rollout/episode_steps'] = np.mean(epoch_episode_steps)
        combined_stats['rollout/actions_mean'] = np.mean(epoch_actions)
        combined_stats['rollout/Q_mean'] = np.mean(epoch_qs)
        combined_stats['train/loss_actor'] = np.mean(epoch_actor_losses)
        combined_stats['train/loss_critic'] = np.mean(epoch_critic_losses)
        combined_stats['train/param_noise_distance'] = np.mean(epoch_adaptive_distances)
        combined_stats['total/duration'] = duration
        combined_stats['total/steps_per_second'] = float(t) / float(duration)
        combined_stats['total/episodes'] = episodes
        combined_stats['rollout/episodes'] = epoch_episodes
        combined_stats['rollout/actions_std'] = np.std(epoch_actions)

        mean_epoch_episode_rewards.append(np.mean(epoch_episode_rewards))
        step_set.append(t)
        plt.plot(step_set,mean_epoch_episode_rewards)
        plt.xlabel('Steps')
        plt.ylabel('Mean Episode Reward')
        plt.savefig('ddpg_mean_test.png')
        # plt.show()

        # Evaluation statistics.
        if eval_env is not None:
            combined_stats['eval/return'] = eval_episode_rewards
            combined_stats['eval/return_history'] = np.mean(eval_episode_rewards_history)
            combined_stats['eval/Q'] = eval_qs
            combined_stats['eval/episodes'] = len(eval_episode_rewards)
        def as_scalar(x):
            if isinstance(x, np.ndarray):
                assert x.size == 1
                return x[0]
            elif np.isscalar(x):
                return x
            else:
                raise ValueError('expected scalar, got %s'%x)

        combined_stats_sums = MPI.COMM_WORLD.allreduce(np.array([ np.array(x).flatten()[0] for x in combined_stats.values()]))
        combined_stats = {k : v / mpi_size for (k,v) in zip(combined_stats.keys(), combined_stats_sums)}

        # Total statistics.
        combined_stats['total/epochs'] = epoch + 1
        combined_stats['total/steps'] = t

        for key in sorted(combined_stats.keys()):
            logger.record_tabular(key, combined_stats[key])

        if rank == 0:
            logger.dump_tabular()
        logger.info('')
        logdir = logger.get_dir()
        if rank == 0 and logdir:
            if hasattr(env, 'get_state'):
                with open(os.path.join(logdir, 'env_state.pkl'), 'wb') as f:
                    pickle.dump(env.get_state(), f)
            if eval_env and hasattr(eval_env, 'get_state'):
                with open(os.path.join(logdir, 'eval_env_state.pkl'), 'wb') as f:
                    pickle.dump(eval_env.get_state(), f)


    return agent




def retraining(save_path, network, env,
          seed=None,
          total_timesteps=None,
          nb_epochs=None, # with default settings, perform 1M steps total
          nb_epoch_cycles=50,
          nb_rollout_steps=3,  #100
          reward_scale=1.0,
          render=False,
          render_eval=False,
        #   noise_type='adaptive-param_0.2',
          noise_type='normal_0.2',
        #   noise_type='ou_0.9',

          normalize_returns=False,
          normalize_observations=True,
          critic_l2_reg=1e-2,
          actor_lr=1e-4,
          critic_lr=1e-3,
        #   actor_lr=1e-6,
        #   critic_lr=1e-5,
          popart=False,
          gamma=0.99,
          clip_norm=None,
          nb_train_steps=3, # per epoch cycle and MPI worker,  50
          nb_eval_steps=1,  #100
          batch_size=640, # per MPI worker
          tau=0.01,
          eval_env=None,
          param_noise_adaption_interval=3, #50
          **network_kwargs):


    if total_timesteps is not None:
        assert nb_epochs is None
        nb_epochs = int(total_timesteps) // (nb_epoch_cycles * nb_rollout_steps)
    else:
        nb_epochs = 500

    rank = MPI.COMM_WORLD.Get_rank()
    nb_actions = env.num_actions

    action_shape=np.array(nb_actions*[0]).shape

    nb_features = 2*(env.num_actions+1)+env.num_actions
    observation_shape=np.array(nb_features*[0]).shape
    # assert (np.abs(env.action_space.low) == env.action_space.high).all()  # we assume symmetric actions.

    # memory = Memory(limit=int(1e6), action_shape=env.action_space.shape, observation_shape=env.observation_space.shape)
    memory = Memory(limit=int(1e6), action_shape=action_shape, observation_shape=observation_shape)
    critic = Critic(network=network, **network_kwargs)
    actor = Actor(nb_actions, network=network, **network_kwargs)

    action_noise = None
    param_noise = None
    # nb_actions = env.action_space.shape[-1]
    if noise_type is not None:
        for current_noise_type in noise_type.split(','):
            current_noise_type = current_noise_type.strip()
            if current_noise_type == 'none':
                pass
            elif 'adaptive-param' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                param_noise = AdaptiveParamNoiseSpec(initial_stddev=float(stddev), desired_action_stddev=float(stddev))
            elif 'normal' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                action_noise = NormalActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
            elif 'ou' in current_noise_type:
                _, stddev = current_noise_type.split('_')
                action_noise = OrnsteinUhlenbeckActionNoise(mu=np.zeros(nb_actions), sigma=float(stddev) * np.ones(nb_actions))
            else:
                raise RuntimeError('unknown noise type "{}"'.format(current_noise_type))

    # agent = DDPG(actor, critic, memory, env.observation_space.shape, env.action_space.shape,
    agent = DDPG(actor, critic, memory, observation_shape, action_shape,
        gamma=gamma, tau=tau, normalize_returns=normalize_returns, normalize_observations=normalize_observations,
        batch_size=batch_size, action_noise=action_noise, param_noise=param_noise, critic_l2_reg=critic_l2_reg,
        actor_lr=actor_lr, critic_lr=critic_lr, enable_popart=popart, clip_norm=clip_norm,
        reward_scale=reward_scale)
    logger.info('Using agent with the following configuration:')
    logger.info(str(agent.__dict__.items()))

    eval_episode_rewards_history = deque(maxlen=100)
    episode_rewards_history = deque(maxlen=100)
    sess = U.get_session()
    # Prepare everything.
    agent.load(sess,save_path)
    # sess.graph.finalize()

    agent.reset()

    obs = env.reset()
    if eval_env is not None:
        eval_obs = eval_env.reset()
    nenvs = obs.shape[0]

    episode_reward = np.zeros(nenvs, dtype = np.float32) #vector
    episode_step = np.zeros(nenvs, dtype = int) # vector
    episodes = 0 #scalar
    t = 0 # scalar
    step_set=[]
    reward_set=[]

    epoch = 0



    start_time = time.time()

    epoch_episode_rewards = []
    mean_epoch_episode_rewards = []
    epoch_episode_steps = []
    epoch_actions = []
    epoch_qs = []
    epoch_episodes = 0
    for epoch in range(nb_epochs):
        print(nb_epochs)
        obs = env.reset()
        agent.save(save_path)
        epoch_episode_rewards=[]
        for cycle in range(nb_epoch_cycles):
            # Perform rollouts.

            for t_rollout in range(nb_rollout_steps):
                # Predict next action.
                action, q, _, _ = agent.step(obs, apply_noise=True, compute_Q=True)

                '''actually no need for env_state: in or out'''
                new_obs, r, done = env.step(action)


                # print('reward:', r)
                # note these outputs are batched from vecenv
                # print('obs: ',obs.shape,obs, 'action: ', action.shape, action )
                '''obs shape: (1,17), action shape: (1,6)'''
                # print('maxaction: ', max_action.shape)
                '''max_action shape: (6,) , max_action*action shape: (1,6)'''
                t += 1
                # if rank == 0 and render:
                #     env.render()
                # print('r:', r)
                episode_reward += r
                episode_step += 1
                # print('episode_re: ', episode_reward) #[1.]

                # Book-keeping.
                epoch_actions.append(action)
                epoch_qs.append(q)
                b=1.
                agent.store_transition(obs, action, r, new_obs, done) #the batched data will be unrolled in memory.py's append.
                # print('r: ', r)
                # '''r shape: (1,)'''
                obs = new_obs

                # for d in range(len(done)):
                #     if done[d]:
                #         print('done')
                #         # Episode done.
                #         epoch_episode_rewards.append(episode_reward[d])
                #         episode_rewards_history.append(episode_reward[d])
                #         epoch_episode_steps.append(episode_step[d])
                #         episode_reward[d] = 0.
                #         episode_step[d] = 0
                #         epoch_episodes += 1
                #         episodes += 1
                #         if nenvs == 1:
                #             agent.reset()

            '''added'''                
            epoch_episode_rewards.append(episode_reward)

            episode_reward = np.zeros(nenvs, dtype = np.float32) #vector

            # Train.
            epoch_actor_losses = []
            epoch_critic_losses = []
            epoch_adaptive_distances = []
            for t_train in range(nb_train_steps):
                # Adapt param noise, if necessary.
                if memory.nb_entries >= batch_size and t_train % param_noise_adaption_interval == 0:
                    distance = agent.adapt_param_noise()
                    epoch_adaptive_distances.append(distance)
                # print('Train!')
                cl, al = agent.train()
                epoch_critic_losses.append(cl)
                epoch_actor_losses.append(al)
                agent.update_target_net()

            # Evaluate.
            eval_episode_rewards = []
            eval_qs = []
            if eval_env is not None:
                nenvs_eval = eval_obs.shape[0]
                eval_episode_reward = np.zeros(nenvs_eval, dtype = np.float32)
                for t_rollout in range(nb_eval_steps):
                    eval_action, eval_q, _, _ = agent.step(eval_obs, apply_noise=False, compute_Q=True)
                    # eval_obs, eval_r, eval_done, eval_info = eval_env.step(max_action * eval_action)  # scale for execution in env (as far as DDPG is concerned, every action is in [-1, 1])
                    eval_obs, eval_r, eval_done, eval_info = eval_env.step( eval_action)
                    if render_eval:
                        eval_env.render()
                    eval_episode_reward += eval_r

                    eval_qs.append(eval_q)
                    for d in range(len(eval_done)):
                        if eval_done[d]:
                            eval_episode_rewards.append(eval_episode_reward[d])
                            eval_episode_rewards_history.append(eval_episode_reward[d])
                            eval_episode_reward[d] = 0.0

        mpi_size = MPI.COMM_WORLD.Get_size()
        # Log stats.
        # XXX shouldn't call np.mean on variable length lists
        duration = time.time() - start_time
        stats = agent.get_stats()
        combined_stats = stats.copy()
        combined_stats['rollout/return'] = np.mean(epoch_episode_rewards)
        combined_stats['rollout/return_history'] = np.mean(episode_rewards_history)
        combined_stats['rollout/episode_steps'] = np.mean(epoch_episode_steps)
        combined_stats['rollout/actions_mean'] = np.mean(epoch_actions)
        combined_stats['rollout/Q_mean'] = np.mean(epoch_qs)
        combined_stats['train/loss_actor'] = np.mean(epoch_actor_losses)
        combined_stats['train/loss_critic'] = np.mean(epoch_critic_losses)
        combined_stats['train/param_noise_distance'] = np.mean(epoch_adaptive_distances)
        combined_stats['total/duration'] = duration
        combined_stats['total/steps_per_second'] = float(t) / float(duration)
        combined_stats['total/episodes'] = episodes
        combined_stats['rollout/episodes'] = epoch_episodes
        combined_stats['rollout/actions_std'] = np.std(epoch_actions)

        mean_epoch_episode_rewards.append(np.mean(epoch_episode_rewards))
        step_set.append(t)
        plt.plot(step_set,mean_epoch_episode_rewards)
        plt.xlabel('Steps')
        plt.ylabel('Mean Episode Reward')
        plt.savefig('ddpg_mean_retrain.png')
        # plt.show()

        # Evaluation statistics.
        if eval_env is not None:
            combined_stats['eval/return'] = eval_episode_rewards
            combined_stats['eval/return_history'] = np.mean(eval_episode_rewards_history)
            combined_stats['eval/Q'] = eval_qs
            combined_stats['eval/episodes'] = len(eval_episode_rewards)
        def as_scalar(x):
            if isinstance(x, np.ndarray):
                assert x.size == 1
                return x[0]
            elif np.isscalar(x):
                return x
            else:
                raise ValueError('expected scalar, got %s'%x)

        combined_stats_sums = MPI.COMM_WORLD.allreduce(np.array([ np.array(x).flatten()[0] for x in combined_stats.values()]))
        combined_stats = {k : v / mpi_size for (k,v) in zip(combined_stats.keys(), combined_stats_sums)}

        # Total statistics.
        combined_stats['total/epochs'] = epoch + 1
        combined_stats['total/steps'] = t

        for key in sorted(combined_stats.keys()):
            logger.record_tabular(key, combined_stats[key])

        if rank == 0:
            logger.dump_tabular()
        logger.info('')
        logdir = logger.get_dir()
        if rank == 0 and logdir:
            if hasattr(env, 'get_state'):
                with open(os.path.join(logdir, 'env_state.pkl'), 'wb') as f:
                    pickle.dump(env.get_state(), f)
            if eval_env and hasattr(eval_env, 'get_state'):
                with open(os.path.join(logdir, 'eval_env_state.pkl'), 'wb') as f:
                    pickle.dump(eval_env.get_state(), f)


    return agent
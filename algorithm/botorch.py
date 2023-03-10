"""BO explorer."""
import random
from bisect import bisect_left
from typing import Optional, Tuple
from utils.seq_utils import  sample_new_seqs,levenshtein_distance,check_cdr_constraints

import flexs
import numpy as np
import pandas as pd
from flexs.utils.replay_buffers import PrioritizedReplayBuffer
from flexs.utils.sequence_utils import (
    construct_mutant_from_sample,
    generate_random_sequences,
    one_hot_to_string,
    string_to_one_hot,
)

from . import register_algorithm


@register_algorithm("botorch")
class BO(flexs.Explorer):
    """Evolutionary Bayesian Optimization (Evo_BO) explorer.

    Algorithm works as follows:     for N experiment rounds         recombine samples from previous
    batch if it exists and measure them,             otherwise skip         Thompson sample
    starting sequence for new batch         while less than B samples in batch             Generate
    `model_queries_per_batch/sequences_batch_size` samples             If variance of ensemble
    models is above twice that of the starting                 sequence             Thompson sample
    another starting sequence
    """

    def __init__(
        self,
        args,
        model,
        alphabet: str,
        starting_sequence: str,
    ):
        """
        Args:
            method (equal to EI or UCB): The improvement method used in BO,
                default EI.
            recomb_rate: The recombination rate on the previous batch before
                BO proposes samples, default 0.

        """
        method = "EI"
        name = f"BO_method={method}"
        self.name = name
        self.starting_sequence = starting_sequence
        self.sequences_batch_size = args.num_queries_per_round
        self.rounds = args.num_rounds
        self.model_queries_per_batch = args.num_model_queries_per_round
        self.model = model
        self.alphabet = alphabet
        self.method = "UCB"
        self.recomb_rate = 0.2
        self.best_fitness = 0
        self.num_actions = 0
        self.state = None
        self.seq_len = None
        self.memory = None
        self.initial_uncertainty = None
        self.rng = np.random.default_rng(args.seed)

    def initialize_data_structures(self):
        """Initialize."""
        self.state = string_to_one_hot(self.starting_sequence, self.alphabet)
        self.seq_len = len(self.starting_sequence)
        # use PER buffer, same as in DQN
        self.memory = PrioritizedReplayBuffer(
            len(self.alphabet) * self.seq_len, 100000, self.sequences_batch_size, 0.6
        )

    def train_models(self): ## change reward
        """Train the model."""
        if len(self.memory) >= self.sequences_batch_size:
            batch = self.memory.sample_batch()
        else:
            self.memory.batch_size = len(self.memory)
            batch = self.memory.sample_batch()
            self.memory.batch_size = self.sequences_batch_size
        states = batch["next_obs"]
        state_seqs = [
            one_hot_to_string(state.reshape((-1, len(self.alphabet))), self.alphabet)
            for state in states
        ]
        rewards = batch["rews"]
        
        self.model.train(state_seqs, rewards)
        

    def _recombine_population(self, gen):
        np.random.shuffle(gen)
        ret = []
        for i in range(0, len(gen) - 1, 2):
            strA = []
            strB = []
            switch = False
            for ind in range(len(gen[i])):
                if np.random.random() < self.recomb_rate:
                    switch = not switch

                # putting together recombinants
                if switch:
                    strA.append(gen[i][ind])
                    strB.append(gen[i + 1][ind])
                else:
                    strB.append(gen[i][ind])
                    strA.append(gen[i + 1][ind])

            ret.append("".join(strA))
            ret.append("".join(strB))
        return ret

    def EI(self, vals):
        """Compute expected improvement."""
        # print('vals',vals)
        # return np.mean([max(val - self.best_fitness, 0) for val in vals])
        return np.mean([max(vals - self.best_fitness, 0)])

    @staticmethod
    def UCB(vals, mean_pre, std_pre):
        """Upper confidence bound."""
        discount = 0.01

        return np.mean(vals) + mean_pre - discount * np.std(std_pre)


    def pick_action(self, all_measured_seqs,x_central_local,landscape,all_seqs, threshold=10):
        """Pick action."""
        state = self.state.copy()
        states_to_screen = []
        states_to_screen=[]
        
        ## local search for all satisfied seq candidate pool
        candidate_pool = list(set(all_seqs) - set(all_measured_seqs))
        ## not enough do global search
        if len(candidate_pool)<(self.model_queries_per_batch // self.sequences_batch_size):
            states_to_screen_=sample_new_seqs(
                        all_seqs, all_measured_seqs, (self.model_queries_per_batch // self.sequences_batch_size)-len(candidate_pool), self.rng
                    )
            candidate_pool.extend(states_to_screen_)
            states_to_screen=candidate_pool

        ## enough then we sample from satisfied pool
        else:
            states_to_screen=self.rng.choice(list(candidate_pool), size=self.model_queries_per_batch // self.sequences_batch_size, replace=False)

        ## put it outside
        ensemble_preds = landscape.get_fitness(states_to_screen) ## landscape's fitnesss
        
        mean_pred = np.mean(ensemble_preds)
        std_pre = np.std(ensemble_preds)

        method_pred = (
            [self.EI(vals) for vals in ensemble_preds]
            if self.method == "EI"
            else [self.UCB(vals, mean_pred, std_pre) for vals in ensemble_preds]
        )

        a = np.random.uniform(0, 1) ### print
        a_ = [a] * len(method_pred)
        lists_of_lists = [method_pred, a_]
        method_pred = [sum(x) for x in zip(*lists_of_lists)]
        epsilon = 0.99

        ## this is to do epislon greedy policy
        if a <= epsilon:
            action_ind = np.argmax(method_pred)
        else:
            action_ind = np.random.randint(len(method_pred))


        uncertainty = np.std(method_pred[action_ind])
        action = action_ind
        new_state_string = states_to_screen[action_ind]
        self.state = string_to_one_hot(new_state_string, self.alphabet)
        new_state = self.state
        reward = np.mean(ensemble_preds[action_ind])
        if new_state_string not in all_measured_seqs:
            self.best_fitness = max(self.best_fitness, reward)
            self.memory.store(state.ravel(), action, reward, new_state.ravel()) 
        self.num_actions += 1
        return uncertainty, new_state_string, reward

    @staticmethod
    def Thompson_sample(measured_batch):
        """Pick a sequence via Thompson sampling."""
        fitnesses = np.cumsum(
            [np.exp(1 * x[0]) for x in measured_batch]
        )  # make it small inorder to avoid inf, previously it was 10*x[0]
        fitnesses = fitnesses / fitnesses[-1]
        x = np.random.uniform()
        index = bisect_left(fitnesses, x)
        sequences = [x[1] for x in measured_batch]
        return sequences[index]

    def propose_sequences(
        self, measured_sequences: pd.DataFrame, landscape, **kwargs
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Propose top `sequences_batch_size` sequences for evaluation."""
        if self.num_actions == 0:
            # indicates model was reset
            self.initialize_data_structures()
            x_central_local=self.starting_sequence
        else:
            # set state to best measured sequence from prior batch
            last_round_num = measured_sequences["round"].max()
            last_batch = measured_sequences[measured_sequences["round"] == last_round_num]
            _last_batch_seqs = last_batch["sequence"].tolist()
            _last_batch_true_scores = last_batch["true_score"].tolist()
            last_batch_seqs = _last_batch_seqs
            if self.recomb_rate > 0 and len(last_batch) > 1:
                last_batch_seqs = self._recombine_population(last_batch_seqs)
            measured_batch = []
            for seq in last_batch_seqs:
                if seq in _last_batch_seqs:
                    measured_batch.append(
                        (_last_batch_true_scores[_last_batch_seqs.index(seq)], seq)
                    )
                else:
                    measured_batch.append((np.mean(self.model.get_fitness([seq])), seq))
            measured_batch = sorted(measured_batch)
            sampled_seq = self.Thompson_sample(measured_batch)
            self.state = string_to_one_hot(sampled_seq, self.alphabet)
            max_score_id=np.argmax(_last_batch_true_scores)
            x_central_local = last_batch_seqs[max_score_id]
        # generate next batch by picking actions
        self.initial_uncertainty = None
        samples = set()
        all_measured_seqs = set(measured_sequences["sequence"].tolist())
        prev_cost = len(all_measured_seqs)
        query_cost= prev_cost
        while query_cost - prev_cost < self.model_queries_per_batch:
            uncertainty, new_state_string, _ = self.pick_action(all_measured_seqs,x_central_local,landscape,kwargs["all_seqs"]) ## too slow
            all_measured_seqs.add(new_state_string)
            query_cost= len(all_measured_seqs)
            samples.add(new_state_string)
            if self.initial_uncertainty is None:
                self.initial_uncertainty = uncertainty
            if uncertainty > 2 * self.initial_uncertainty:
                # reset sequence to starting sequence if we're in territory that's too
                # uncharted
                sampled_seq = self.Thompson_sample(measured_batch)
                self.state = string_to_one_hot(sampled_seq, self.alphabet)
                self.initial_uncertainty = None

        if len(samples) < self.sequences_batch_size:
            random_sequences = generate_random_sequences(
                self.seq_len, self.sequences_batch_size - len(samples), self.alphabet
            )
            samples.update(random_sequences)
        # get predicted fitnesses of samples
        samples = list(samples)
        
        preds = self.model.get_fitness(samples)
        print(f'preds is {preds}')
        
        # train ensemble model before returning samples
        self.train_models()

        samples = random.sample(
            samples, self.sequences_batch_size
        ) 
        return samples, preds

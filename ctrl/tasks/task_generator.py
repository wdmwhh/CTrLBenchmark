# Copyright (c) Facebook, Inc. and its affiliates.
# 
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import logging
import os
import random
import time
from types import SimpleNamespace

import numpy as np
import torch
import torch.nn.functional as F
from ctrl.concepts.concept import ComposedConcept
from ctrl.concepts.concept_tree import ConceptTree
from ctrl.tasks.task import Task
from torchvision import transforms

logger = logging.getLogger(__name__)


def loss(y_hat, y, reduction: str = 'none'):
    """

    :param y_hat: Model predictions
    :param y: Ground Truth
    :param reduction:
    :return:
    """
    assert y.size(1) == 1 and torch.is_tensor(y_hat)
    y = y.squeeze(1)
    loss_val = F.cross_entropy(y_hat, y, reduction=reduction)
    assert loss_val.dim() == 1
    return loss_val


def augment_samples(samples):
    trans = transforms.Compose(
        [
            transforms.ToPILImage(),
            transforms.RandomHorizontalFlip(),
            transforms.RandomCrop(32, 4),
            transforms.ToTensor()
        ])
    aug_samples = []
    for sample in samples:
        for i in range(4):
            aug_samples.append(trans(sample))
    for sample in samples:
        aug_samples.append(transforms.ToTensor()(transforms.ToPILImage()(sample)))

    return torch.stack(aug_samples)


def _generate_samples_from_descr(categories, attributes, n_samples_per_class,
                                 augment, rnd):
    use_cat_id, attributes = attributes
    assert use_cat_id and not attributes, \
        "usage of attributes isn't supporte in v1."

    samples = []
    labels = []
    for i, cat_concepts in enumerate(categories):
        mixture = ComposedConcept(cat_concepts, id=None)
        cat_samples = []
        cat_labels = []
        for s_id, n in enumerate(n_samples_per_class):
            split_samples, split_attrs = mixture._get_samples(n, attributes,
                                                              split_id=s_id, rng=rnd)
            if s_id in augment:
                 split_samples = augment_samples(split_samples)
            split_labels = torch.Tensor().long()
            cat_id = torch.tensor([i]).expand(split_samples.shape[0], 1)
            split_labels = torch.cat([split_labels, cat_id], dim=1)

            cat_samples.append(split_samples)
            cat_labels.append(split_labels)
        samples.append(cat_samples)
        labels.append(cat_labels)
    if torch.is_tensor(samples[0][0]):
        cat_func = torch.cat
    else:
        cat_func = np.concatenate
    samples = (cat_func(split) for split in zip(*samples))
    labels = (torch.cat(split) for split in zip(*labels))

    return samples, labels


class TaskGenIter(object):
    def __init__(self, task_generator):
        self.task_gen = task_generator
        self.n = 0

    def __next__(self):
        if len(self.task_gen.task_pool) > self.n:
            t = self.task_gen.task_pool[self.n]
        else:
            assert self.n == len(self.task_gen.task_pool)
            try:
                t = self.task_gen.add_task()
            except IndexError:
                raise StopIteration
        self.n += 1
        return t


class TaskGenerator(object):
    def __init__(self, concept_pool: ConceptTree, transformation_pool,
                 samples_per_class, split_names, strat,
                 seed: int, flatten, n_initial_classes, use_cat_id, tta,
                 *args, **kwargs):
        """

        :param concepts: Concept pool from which we will sample when creating
            new tasks.
        :param transformation_pool: Transformation pool from which we will
            select the operations to be applied on  the data of new tasks.
        :param samples_per_class: Initial number of samples per class
        :param split_names: Name of the different data splits usually
            (train, val, test)
        :param strat: Strategy to use for the creation of new tasks
        :param seed: The seed used for the samples selection
        :param flatten:
        :param n_initial_classes:
        :param use_cat_id: Legacy prop used with attributes.
        :param tta: use Test Time Augmentation
        """
        self.task_pool = []

        self.concept_pool = concept_pool
        self.transformation_pool = transformation_pool
        assert len(samples_per_class) == len(split_names)
        self.n_samples_per_class = samples_per_class
        self.split_names = split_names

        self.rnd = random.Random(seed)

        self.flatten = flatten
        self.tta = tta

        # For default task creation
        self.n_initial_classes = n_initial_classes
        self.use_cat_id = use_cat_id

        self.strat = strat
        self.contains_loaded_tasks = False

    @property
    def n_tasks(self):
        return len(self.task_pool)

    def add_task(self, name=None, save_path=None):
        """
        Adds a new task to the current pool.
        This task will be created using the current strategy `self.strat`
        :param name: The name of the new task
        :param save_path: If provided, the task will be saved under this path
        :return: The new Task
        """
        new_task_id = len(self.task_pool)

        if new_task_id == 0:
            concepts, attrs, trans, n = self._create_new_task(
                self.concept_pool, self.transformation_pool)
        else:
            concepts = self.task_pool[-1].src_concepts
            attrs = self.task_pool[-1].attributes
            trans = self.task_pool[-1].transformation
            n = self.task_pool[-1].n_samples_per_class

        cur_task_spec = SimpleNamespace(src_concepts=concepts,
                                        attributes=attrs,
                                        transformation=trans,
                                        n_samples_per_class=n,
                                        )

        cur_task_spec = self.strat.new_task(cur_task_spec, self.concept_pool,
                                       self.transformation_pool,
                                       self.task_pool)
        assert len(cur_task_spec.n_samples_per_class) == len(self.split_names)

        new_task = self._create_task(cur_task_spec, name, save_path)
        new_task.id = new_task_id
        self.task_pool.append(new_task)
        return new_task

    def load_task(self, task_name, load_path):
        splits = ['train', 'val', 'test']
        samples = []
        save_paths = []
        for split in splits:
            file_path = os.path.join(load_path, '{}_{}.pth'.format(task_name, split))
            save_paths.append(file_path)
            assert os.path.isfile(file_path), file_path
            xs, ys = torch.load(file_path)
            samples.append((xs, ys))
        metadata_file = os.path.join(load_path, '{}.meta'.format(task_name))
        if os.path.isfile(metadata_file):
            meta = torch.load(metadata_file)
        else:
            meta = {}
        task = Task(task_name, samples, loss, split_names=self.split_names,
                    id=len(self.task_pool), **meta)
        task.save_path = save_paths
        self.task_pool.append(task)
        self.contains_loaded_tasks = True
        return task

    def _create_task(self, task_spec, name, save_path):
        concepts = task_spec.src_concepts
        attributes = task_spec.attributes
        transformation = task_spec.transformation
        n_samples_per_class = task_spec.n_samples_per_class

        samples = self.get_samples(concepts, attributes, transformation,
                                   n_samples_per_class)
        if self.flatten:
            samples = [(x.view(x.size(0), -1), y) for x, y in samples]
        task = Task(name, samples, loss, transformation, self.split_names,
                    source_concepts=concepts, attributes=attributes,
                    creator=self.strat.descr(), generator=self,
                    n_samples_per_class=n_samples_per_class,
                    save_path=save_path)
        return task

    def get_similarities(self, component=None):
        """
        :param component: String representing the components across which the
            similarities should be computed, can be any combination of :

            - 'x' for p(x|z)
            - 'y' for p(y|z)
            - 'z' for p(z)
        :return: A dict associating each component to an n_tasks x n_tasks
         tensor containing the similarities between tasks over this component.
        """
        if component is None:
            component = 'xyz'

        similarities = torch.zeros(self.n_tasks, self.n_tasks, len(component))
        times = torch.zeros(len(component))
        for i, t1 in enumerate(self.task_pool):
            for j, t2 in enumerate(self.task_pool[i:]):
                sim, time = self.get_similarity(t1, t2, component)
                sim = torch.tensor(sim)
                # Similarities are symmetric
                similarities[i, i + j] = sim
                similarities[i + j, i] = sim
                times += torch.tensor(time)
        for comp, time in zip(component, times.unbind()):
            if time > 1:
                logger.warning(
                    "Comparison of {} took {:4.2f}s".format(comp, time))

        sim_dict = dict(zip(component, similarities.unbind(-1)))
        return sim_dict

    def get_similarity(self, t1, t2, component=None):
        if component is None:
            component = 'xyz'
        res = []
        times = []
        for char in component:
            start_time = time.time()
            if char == 'x':
                res.append(self.transformation_pool.transformations_sim(
                    t1.transformation, t2.transformation))
            elif char == 'y':
                res.append(self.concept_pool.y_attributes_sim(t1.attributes,
                                                              t2.attributes))
            elif char == 'z':
                res.append(self.concept_pool.categories_sim(t1.src_concepts,
                                                            t2.src_concepts))
            else:
                raise ValueError('Unknown component {}'.format(char))
            times.append(time.time() - start_time)
        return res, times

    def get_samples(self, concepts, attributes, transformation,
                    n_samples_per_class):
        augment = [1] if self.tta else []
        samples, labels = _generate_samples_from_descr(concepts, attributes,
                                                       n_samples_per_class,
                                                       augment, np.random.default_rng(self.rnd.randint(0, int(1e9))))
        # Apply the input transformation
        samples = [transformation(x) for x in samples]

        return [(x, y) for x, y in zip(samples, labels)]

    def stream_infos(self, full=True):
        """
        return a list containing the information of each task in the task_pool,
        useful when the stream needs to be serialized (e.g. to be sent to
        workers.)
        """
        return [t.info(full) for t in self.task_pool]

    def _create_new_task(self, concept_pool, transformation_pool, n_attributes=0):
        logger.info('Creating new task from scratch')
        concepts = concept_pool.get_compatible_concepts(self.n_initial_classes,
                                                        leaf_only=True,)

        n_avail_attrs = len(concept_pool.attributes)
        if n_attributes > n_avail_attrs:
            raise ValueError('Can\'t select {} attributes, only {} available'
                             .format(n_attributes, n_avail_attrs))
        attributes = self.rnd.sample(range(n_avail_attrs), n_attributes)

        transformation = transformation_pool.get_transformation()
        concepts = [(c,) for c in concepts]

        return concepts, (self.use_cat_id, attributes), transformation, \
               self.n_samples_per_class

    def __str__(self):
        descr = "Task stream containing {} tasks:\n\t".format(self.n_tasks)
        tasks = '\n\t'.join(map(str, self.task_pool))
        return descr + tasks

    def __iter__(self):
        return TaskGenIter(self)


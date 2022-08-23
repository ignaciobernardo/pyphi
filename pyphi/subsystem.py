#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# subsystem.py

"""Represents a candidate system for |small_phi| and |big_phi| evaluation."""

import functools
import logging
from math import log2
from typing import Iterable

import numpy as np

from . import cache, connectivity, convert, distribution, resolve_ties, utils, validate
from .conf import config, fallback
from .direction import Direction
from .distribution import max_entropy_distribution, repertoire_shape
from .metrics.distribution import repertoire_distance as _repertoire_distance
from .models import (
    Concept,
    MaximallyIrreducibleCause,
    MaximallyIrreducibleEffect,
    NullCut,
    RepertoireIrreducibilityAnalysis,
    _null_ria,
)
from .network import irreducible_purviews
from .node import generate_nodes
from .partition import complete_partition, mip_partitions
from .tpm import condition_tpm, marginalize_out
from .utils import state_of

log = logging.getLogger(__name__)


class Subsystem:
    """A set of nodes in a network.

    Args:
        network (Network): The network the subsystem belongs to.
        state (tuple[int]): The state of the network.

    Keyword Args:
        nodes (tuple[int] or tuple[str]): The nodes of the network which are in
            this subsystem. Nodes can be specified either as indices or as
            labels if the |Network| was passed ``node_labels``. If this is
            ``None`` then the full network will be used.
        cut (Cut): The unidirectional |Cut| to apply to this subsystem.

    Attributes:
        network (Network): The network the subsystem belongs to.
        tpm (np.ndarray): The TPM conditioned on the state of the external
            nodes.
        cm (np.ndarray): The connectivity matrix after applying the cut.
        state (tuple[int]): The state of the network.
        node_indices (tuple[int]): The indices of the nodes in the subsystem.
        cut (Cut): The cut that has been applied to this subsystem. Defaults to
            the null cut.
    """

    def __init__(
        self,
        network,
        state,
        nodes=None,
        cut=None,
        mice_cache=None,
        repertoire_cache=None,
        single_node_repertoire_cache=None,
        repertoire_nonvirtualized_cache=None,
        _external_indices=None,
    ):
        # The network this subsystem belongs to.
        validate.is_network(network)
        self.network = network

        self.node_labels = network.node_labels
        # Remove duplicates, sort, and ensure native Python `int`s
        # (for JSON serialization).
        self.node_indices = self.node_labels.coerce_to_indices(nodes)

        validate.state_length(state, self.network.size)

        # The state of the network.
        self.state = tuple(state)

        # Get the external node indices.
        # TODO: don't expose this as an attribute?
        if _external_indices is None:
            self.external_indices = tuple(
                set(network.node_indices) - set(self.node_indices)
            )
        else:
            self.external_indices = _external_indices

        # The TPM conditioned on the state of the external nodes.
        self.tpm = condition_tpm(self.network.tpm, self.external_indices, self.state)
        # The TPM for just the nodes in the subsystem.
        self.proper_tpm = self.tpm.squeeze()[..., list(self.node_indices)]

        # The unidirectional cut applied for phi evaluation
        self.cut = (
            cut if cut is not None else NullCut(self.node_indices, self.node_labels)
        )

        # The network's connectivity matrix with cut applied
        self.cm = self.cut.apply_cut(network.cm)
        # The subsystem's connectivity matrix with the cut applied
        self.proper_cm = connectivity.subadjacency(self.cm, self.node_indices)

        # Reusable cache for maximally-irreducible causes and effects
        self._mice_cache = cache.MICECache(self, mice_cache)

        # Cause & effect repertoire caches
        # TODO: if repertoire caches are never reused, there's no reason to
        # have an accesible object-level cache. Just use a simple memoizer
        self._single_node_repertoire_cache = (
            single_node_repertoire_cache or cache.DictCache()
        )
        self._repertoire_cache = repertoire_cache or cache.DictCache()
        self._repertoire_nonvirtualized_cache = (
            repertoire_nonvirtualized_cache or cache.DictCache()
        )

        self.nodes = generate_nodes(
            self.tpm, self.cm, self.state, self.node_indices, self.node_labels
        )

        validate.subsystem(self)

    @property
    def nodes(self):
        """tuple[Node]: The nodes in this |Subsystem|."""
        return self._nodes

    @nodes.setter
    def nodes(self, value):
        """Remap indices to nodes whenever nodes are changed, e.g. in the
        `macro` module.
        """
        # pylint: disable=attribute-defined-outside-init
        self._nodes = value
        self._index2node = {node.index: node for node in self._nodes}

    @property
    def proper_state(self):
        """tuple[int]: The state of the subsystem.

        ``proper_state[i]`` gives the state of the |ith| node **in the
        subsystem**. Note that this is **not** the state of ``nodes[i]``.
        """
        return utils.state_of(self.node_indices, self.state)

    @property
    def connectivity_matrix(self):
        """np.ndarray: Alias for |Subsystem.cm|."""
        return self.cm

    @property
    def size(self):
        """int: The number of nodes in the subsystem."""
        return len(self.node_indices)

    @property
    def is_cut(self):
        """bool: ``True`` if this Subsystem has a cut applied to it."""
        return not self.cut.is_null

    @property
    def cut_indices(self):
        """tuple[int]: The nodes of this subsystem to cut for |big_phi|
        computations.

        This was added to support ``MacroSubsystem``, which cuts indices other
        than ``node_indices``.

        Yields:
            tuple[int]
        """
        return self.node_indices

    @property
    def cut_mechanisms(self):
        """list[tuple[int]]: The mechanisms that are cut in this system."""
        return self.cut.all_cut_mechanisms()

    @property
    def cut_node_labels(self):
        """``NodeLabels``: Labels for the nodes of this system that will be
        cut.
        """
        return self.node_labels

    @property
    def tpm_size(self):
        """int: The number of nodes in the TPM."""
        return self.tpm.shape[-1]

    def cache_info(self):
        """Report repertoire cache statistics."""
        return {
            "single_node_repertoire": self._single_node_repertoire_cache.info(),
            "repertoire": self._repertoire_cache.info(),
            "mice": self._mice_cache.info(),
        }

    def clear_caches(self):
        """Clear the mice and repertoire caches."""
        self._single_node_repertoire_cache.clear()
        self._repertoire_cache.clear()
        self._mice_cache.clear()

    def __repr__(self):
        return "Subsystem(" + ", ".join(map(repr, self.nodes)) + ")"

    def __str__(self):
        return repr(self)

    def __bool__(self):
        """Return ``False`` if the Subsystem has no nodes, ``True``
        otherwise.
        """
        return bool(self.nodes)

    def __eq__(self, other):
        """Return whether this Subsystem is equal to the other object.

        Two Subsystems are equal if their sets of nodes, networks, and cuts are
        equal.
        """
        if not isinstance(other, Subsystem):
            return False

        return (
            set(self.node_indices) == set(other.node_indices)
            and self.state == other.state
            and self.network == other.network
            and self.cut == other.cut
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __lt__(self, other):
        """Return whether this subsystem has fewer nodes than the other."""
        return len(self.nodes) < len(other.nodes)

    def __gt__(self, other):
        """Return whether this subsystem has more nodes than the other."""
        return len(self.nodes) > len(other.nodes)

    def __le__(self, other):
        return len(self.nodes) <= len(other.nodes)

    def __ge__(self, other):
        return len(self.nodes) >= len(other.nodes)

    def __len__(self):
        """Return the number of nodes in this Subsystem."""
        return len(self.node_indices)

    def __hash__(self):
        return hash((self.network, self.node_indices, self.state, self.cut))

    def to_json(self):
        """Return a JSON-serializable representation."""
        return {
            "network": self.network,
            "state": self.state,
            "nodes": self.node_indices,
            "cut": self.cut,
        }

    def apply_cut(self, cut):
        """Return a cut version of this |Subsystem|.

        Args:
            cut (Cut): The cut to apply to this |Subsystem|.

        Returns:
            Subsystem: The cut subsystem.
        """
        return Subsystem(
            self.network,
            self.state,
            self.node_indices,
            cut=cut,
            mice_cache=self._mice_cache,
        )

    def indices2nodes(self, indices):
        """Return |Nodes| for these indices.

        Args:
            indices (tuple[int]): The indices in question.

        Returns:
            tuple[Node]: The |Node| objects corresponding to these indices.

        Raises:
            ValueError: If requested indices are not in the subsystem.
        """
        if set(indices) - set(self.node_indices):
            raise ValueError("`indices` must be a subset of the Subsystem's indices.")
        return tuple(self._index2node[n] for n in indices)

    # TODO extend to nonbinary nodes
    @cache.method("_single_node_repertoire_cache", Direction.CAUSE)
    def _single_node_cause_repertoire(self, mechanism_node_index, purview):
        # pylint: disable=missing-docstring
        mechanism_node = self._index2node[mechanism_node_index]
        # We're conditioning on this node's state, so take the TPM for the node
        # being in that state.
        tpm = mechanism_node.tpm[..., mechanism_node.state]
        # Marginalize-out all parents of this mechanism node that aren't in the
        # purview.
        return marginalize_out((mechanism_node.inputs - purview), tpm)

    # TODO extend to nonbinary nodes
    @cache.method("_repertoire_cache", Direction.CAUSE)
    def _cause_repertoire(self, mechanism, purview):
        """Return the cause repertoire of a mechanism over a purview.

        Args:
            mechanism (tuple[int]): The mechanism for which to calculate the
                cause repertoire.
            purview (tuple[int]): The purview over which to calculate the
                cause repertoire.

        Returns:
            np.ndarray: The cause repertoire of the mechanism over the purview.

        .. note::
            The returned repertoire is a distribution over purview node states,
            not the states of the whole network.
        """
        # If the purview is empty, the distribution is empty; return the
        # multiplicative identity.
        if not purview:
            return np.array([1.0])
        # If the mechanism is empty, nothing is specified about the previous
        # state of the purview; return the purview's maximum entropy
        # distribution.
        if not mechanism:
            return max_entropy_distribution(purview, self.tpm_size)
        # Use a frozenset so the arguments to `_single_node_cause_repertoire`
        # can be hashed and cached.
        purview = frozenset(purview)
        # Preallocate the repertoire with the proper shape, so that
        # probabilities are broadcasted appropriately.
        joint = np.ones(repertoire_shape(purview, self.tpm_size))
        # The cause repertoire is the product of the cause repertoires of the
        # individual nodes.
        joint *= functools.reduce(
            np.multiply,
            [self._single_node_cause_repertoire(m, purview) for m in mechanism],
        )
        # The resulting joint distribution is over previous states, which are
        # rows in the TPM, so the distribution is a column. The columns of a
        # TPM don't necessarily sum to 1, so we normalize.
        return distribution.normalize(joint)

    @functools.wraps(_cause_repertoire)
    def cause_repertoire(self, mechanism, purview, **kwargs):
        # Drop kwargs
        return self._cause_repertoire(mechanism, purview)

    # TODO extend to nonbinary nodes
    @cache.method("_single_node_repertoire_cache", Direction.EFFECT)
    def _single_node_effect_repertoire(
        self, mechanism: frozenset[int], purview_node_index: int
    ):
        # pylint: disable=missing-docstring
        purview_node = self._index2node[purview_node_index]
        # Condition on the state of the inputs that are in the mechanism.
        mechanism_inputs = purview_node.inputs & mechanism
        tpm = condition_tpm(purview_node.tpm, mechanism_inputs, self.state)
        # Marginalize-out the inputs that aren't in the mechanism.
        nonmechanism_inputs = purview_node.inputs - mechanism
        tpm = marginalize_out(nonmechanism_inputs, tpm)
        # Reshape so that the distribution is over next states.
        return tpm.reshape(repertoire_shape([purview_node.index], self.tpm_size))

    @cache.method("_repertoire_cache", Direction.EFFECT)
    def _effect_repertoire_virtualized(
        self, mechanism: frozenset[int], purview: tuple[int]
    ):
        """Return the effect repertoire of a mechanism over a purview.

        Args:
            mechanism (tuple[int]): The mechanism for which to calculate the
                effect repertoire.
            purview (tuple[int]): The purview over which to calculate the
                effect repertoire.

        Returns:
            np.ndarray: The effect repertoire of the mechanism over the
            purview.

        .. note::
            The returned repertoire is a distribution over purview node states,
            not the states of the whole network.
        """
        # pylint: disable=missing-docstring
        # Preallocate the repertoire with the proper shape, so that
        # probabilities are broadcasted appropriately.
        joint = np.ones(repertoire_shape(purview, self.tpm_size))
        # The effect repertoire is the product of the effect repertoires of the
        # individual nodes.
        return joint * functools.reduce(
            np.multiply,
            [self._single_node_effect_repertoire(mechanism, p) for p in purview],
        )

    # TODO extend to nonbinary nodes
    @cache.method("_repertoire_nonvirtualized_cache", Direction.EFFECT)
    def _effect_repertoire_nonvirtualized(
        self,
        mechanism: frozenset[int],
        purview: tuple[int],
        nonvirtualized_units: frozenset[int],
    ):
        # First, marginalize out virtualized nonmechanism units as normal.
        virtualized_units = set(self.node_indices) - mechanism - nonvirtualized_units
        tpm = marginalize_out(virtualized_units, self.tpm)
        # Ignore any units outside the purview.
        tpm = tpm[..., list(purview)]
        # Convert to state-by-state to get explicit joint probabilities.
        joint = convert.sbn2sbs(tpm)
        # Reshape to multidimensional form.
        n_prev = int(log2(joint.shape[0]))
        joint = convert.sbs_to_multidimensional(joint)
        # Condition on the state of the mechanism.
        mechanism_state = utils.state_of(mechanism, self.state)
        joint = joint[mechanism_state]
        # Marginalize over non-mechanism states.
        previous_state_axes = tuple(range(n_prev))
        joint = joint.sum(axis=previous_state_axes)
        # Renormalize, since we summed along rows.
        joint /= joint.sum()
        # Reshape into a multidimensional repertoire.
        return joint.reshape(repertoire_shape(purview, self.tpm_size))

    def effect_repertoire(self, mechanism, purview, nonvirtualized_units=()):
        # If the purview is empty, the distribution is empty, so return the
        # multiplicative identity.
        if not purview:
            return np.array([1.0])
        # Use a frozenset so the arguments to `_single_node_effect_repertoire`
        # can be hashed and cached.
        mechanism = frozenset(mechanism)
        if not nonvirtualized_units:
            return self._effect_repertoire_virtualized(mechanism, purview)
        nonvirtualized_units = frozenset(nonvirtualized_units)
        return self._effect_repertoire_nonvirtualized(
            mechanism, purview, nonvirtualized_units
        )

    def repertoire(self, direction, mechanism, purview, **kwargs):
        """Return the cause or effect repertoire based on a direction.

        Args:
            direction (Direction): |CAUSE| or |EFFECT|.
            mechanism (tuple[int]): The mechanism for which to calculate the
                repertoire.
            purview (tuple[int]): The purview over which to calculate the
                repertoire.

        Returns:
            np.ndarray: The cause or effect repertoire of the mechanism over
            the purview.

        Raises:
            ValueError: If ``direction`` is invalid.
        """
        if direction == Direction.CAUSE:
            return self.cause_repertoire(mechanism, purview, **kwargs)
        elif direction == Direction.EFFECT:
            return self.effect_repertoire(mechanism, purview, **kwargs)

        return validate.direction(direction)

    def unconstrained_repertoire(self, direction, purview, **kwargs):
        """Return the unconstrained cause/effect repertoire over a purview."""
        return self.repertoire(direction, (), purview, **kwargs)

    def unconstrained_cause_repertoire(self, purview, **kwargs):
        """Return the unconstrained cause repertoire for a purview.

        This is just the cause repertoire in the absence of any mechanism.
        """
        return self.unconstrained_repertoire(Direction.CAUSE, purview, **kwargs)

    def unconstrained_effect_repertoire(self, purview, **kwargs):
        """Return the unconstrained effect repertoire for a purview.

        This is just the effect repertoire in the absence of any mechanism.
        """
        return self.unconstrained_repertoire(Direction.EFFECT, purview, **kwargs)

    def partitioned_repertoire(self, direction, partition, **kwargs):
        """Compute the repertoire of a partitioned mechanism and purview."""
        repertoires = [
            self.repertoire(direction, part.mechanism, part.purview, **kwargs)
            for part in partition
        ]
        return functools.reduce(np.multiply, repertoires)

    def expand_repertoire(self, direction, repertoire, new_purview=None):
        """Distribute an effect repertoire over a larger purview.

        Args:
            direction (Direction): |CAUSE| or |EFFECT|.
            repertoire (np.ndarray): The repertoire to expand.

        Keyword Args:
            new_purview (tuple[int]): The new purview to expand the repertoire
                over. If ``None`` (the default), the new purview is the entire
                network.

        Returns:
            np.ndarray: A distribution over the new purview, where probability
            is spread out over the new nodes.

        Raises:
            ValueError: If the expanded purview doesn't contain the original
                purview.
        """
        if repertoire is None:
            return None

        purview = distribution.purview(repertoire)

        if new_purview is None:
            new_purview = self.node_indices  # full subsystem

        if not set(purview).issubset(new_purview):
            raise ValueError("Expanded purview must contain original purview.")

        # Get the unconstrained repertoire over the other nodes in the network.
        non_purview_indices = tuple(set(new_purview) - set(purview))
        uc = self.unconstrained_repertoire(direction, non_purview_indices)
        # Multiply the given repertoire by the unconstrained one to get a
        # distribution over all the nodes in the network.
        expanded_repertoire = repertoire * uc

        return distribution.normalize(expanded_repertoire)

    def expand_cause_repertoire(self, repertoire, new_purview=None):
        """Alias for |expand_repertoire()| with ``direction`` set to |CAUSE|."""
        return self.expand_repertoire(Direction.CAUSE, repertoire, new_purview)

    def expand_effect_repertoire(self, repertoire, new_purview=None):
        """Alias for |expand_repertoire()| with ``direction`` set to |EFFECT|."""
        return self.expand_repertoire(Direction.EFFECT, repertoire, new_purview)

    def cause_info(self, mechanism, purview, **kwargs):
        """Return the cause information for a mechanism over a purview."""
        return _repertoire_distance(
            self.cause_repertoire(mechanism, purview),
            self.unconstrained_cause_repertoire(purview),
            direction=Direction.CAUSE,
            **kwargs,
        )

    def effect_info(self, mechanism, purview, **kwargs):
        """Return the effect information for a mechanism over a purview."""
        return _repertoire_distance(
            self.effect_repertoire(mechanism, purview),
            self.unconstrained_effect_repertoire(purview),
            direction=Direction.EFFECT,
            **kwargs,
        )

    def cause_effect_info(self, mechanism, purview, **kwargs):
        """Return the cause-effect information for a mechanism over a purview.

        This is the minimum of the cause and effect information.
        """
        return min(
            self.cause_info(mechanism, purview, **kwargs),
            self.effect_info(mechanism, purview, **kwargs),
        )

    # MIP methods
    # =========================================================================

    def evaluate_partition(
        self,
        direction,
        mechanism,
        purview,
        partition,
        repertoire=None,
        repertoire_distance=None,
        return_unpartitioned_repertoire=False,
        partitioned_repertoire_kwargs=None,
        **kwargs,
    ):
        """Return the |small_phi| of a mechanism over a purview for the given
        partition.

        Args:
            direction (Direction): |CAUSE| or |EFFECT|.
            mechanism (tuple[int]): The nodes in the mechanism.
            purview (tuple[int]): The nodes in the purview.
            partition (Bipartition): The partition to evaluate.

        Keyword Args:
            repertoire (np.array): The unpartitioned repertoire.
                If not supplied, it will be computed.

        Returns:
            tuple[int, np.ndarray]: The distance between the unpartitioned and
            partitioned repertoires, and the partitioned repertoire.
        """
        if repertoire is None:
            repertoire = self.repertoire(direction, mechanism, purview)

        partitioned_repertoire_kwargs = partitioned_repertoire_kwargs or dict()
        partitioned_repertoire = self.partitioned_repertoire(
            direction, partition, **partitioned_repertoire_kwargs
        )

        phi = _repertoire_distance(
            repertoire,
            partitioned_repertoire,
            direction=direction,
            repertoire_distance=repertoire_distance,
            **kwargs,
        )

        if return_unpartitioned_repertoire:
            return (phi, partitioned_repertoire, repertoire)
        return (phi, partitioned_repertoire)

    def find_mip(self, direction, mechanism, purview, **kwargs):
        """Return the minimum information partition for a mechanism over a
        purview.

        Args:
            direction (Direction): |CAUSE| or |EFFECT|.
            mechanism (tuple[int]): The nodes in the mechanism.
            purview (tuple[int]): The nodes in the purview.

        Returns:
            RepertoireIrreducibilityAnalysis: The irreducibility analysis for
            the mininum-information partition in one temporal direction.
        """
        if not purview:
            return _null_ria(direction, mechanism, purview)

        # Calculate the unpartitioned repertoire to compare against the
        # partitioned ones.
        repertoire = self.repertoire(direction, mechanism, purview)

        def _mip(phi, partition, partitioned_repertoire):
            # Prototype of MIP with already known data
            # TODO: Use properties here to infer mechanism and purview from
            # partition yet access them with `.mechanism` and `.purview`.
            return RepertoireIrreducibilityAnalysis(
                phi=phi,
                direction=direction,
                mechanism=mechanism,
                purview=purview,
                partition=partition,
                repertoire=repertoire,
                partitioned_repertoire=partitioned_repertoire,
                mechanism_state=state_of(mechanism, self.state),
                purview_state=state_of(purview, self.state),
                node_labels=self.node_labels,
            )

        # State is unreachable - return 0 instead of giving nonsense results
        if direction == Direction.CAUSE and np.all(repertoire == 0):
            return _mip(0, None, None)

        mip = _null_ria(direction, mechanism, purview, phi=float("inf"))

        for partition in mip_partitions(mechanism, purview, self.node_labels):
            # Find the distance between the unpartitioned and partitioned
            # repertoire.
            phi, partitioned_repertoire = self.evaluate_partition(
                direction,
                mechanism,
                purview,
                partition,
                repertoire=repertoire,
                **kwargs,
            )

            # Return immediately if mechanism is reducible.
            if phi == 0:
                return _mip(0.0, partition, partitioned_repertoire)

            # Update MIP if it's more minimal.
            if phi < mip.phi:
                mip = _mip(phi, partition, partitioned_repertoire)

        return mip

    def cause_mip(self, mechanism, purview, **kwargs):
        """Return the irreducibility analysis for the cause MIP.

        Alias for |find_mip()| with ``direction`` set to |CAUSE|.
        """
        return self.find_mip(Direction.CAUSE, mechanism, purview, **kwargs)

    def effect_mip(self, mechanism, purview, **kwargs):
        """Return the irreducibility analysis for the effect MIP.

        Alias for |find_mip()| with ``direction`` set to |EFFECT|.
        """
        return self.find_mip(Direction.EFFECT, mechanism, purview, **kwargs)

    def phi_cause_mip(self, mechanism, purview, **kwargs):
        """Return the |small_phi| of the cause MIP.

        This is the distance between the unpartitioned cause repertoire and the
        MIP cause repertoire.
        """
        mip = self.cause_mip(mechanism, purview, **kwargs)
        return mip.phi if mip else 0

    def phi_effect_mip(self, mechanism, purview, **kwargs):
        """Return the |small_phi| of the effect MIP.

        This is the distance between the unpartitioned effect repertoire and
        the MIP cause repertoire.
        """
        mip = self.effect_mip(mechanism, purview, **kwargs)
        return mip.phi if mip else 0

    def phi(self, mechanism, purview, **kwargs):
        """Return the |small_phi| of a mechanism over a purview."""
        return min(
            self.phi_cause_mip(mechanism, purview, **kwargs),
            self.phi_effect_mip(mechanism, purview, **kwargs),
        )

    # Maximal state methods
    # =========================================================================

    def _specified_states_to_specified_index(self, states, purview):
        full_index = [np.zeros(len(states), dtype=int) for i in self.node_indices]
        specified_indices = states.transpose()
        for i, index in zip(purview, specified_indices):
            full_index[i] = index
        return tuple(full_index)

    def find_maximally_irreducible_state(self, direction, mechanism, purview):
        required_repertoire_distances = [
            "IIT_4.0_SMALL_PHI",
            "IIT_4.0_SMALL_PHI_NO_ABSOLUTE_VALUE",
        ]
        if config.REPERTOIRE_DISTANCE not in required_repertoire_distances:
            raise ValueError(
                f'REPERTOIRE_DISTANCE must be set to one of "{required_repertoire_distances}"'
            )

        state_to_mip = {
            state: self.find_mip(direction, mechanism, purview, state=state)
            for state in utils.all_states(len(purview))
        }
        _, max_mip = max(state_to_mip.items())

        # Record ties
        tied_states, tied_mips = zip(
            *(
                (state, mip)
                for state, mip in state_to_mip.items()
                if mip.phi == max_mip.phi
            )
        )
        tied_states = np.array(tied_states)
        tied_index = self._specified_states_to_specified_index(tied_states, purview)
        for mip in tied_mips:
            # TODO change definition of specified state
            mip._specified_state = tied_states
            mip._specified_index = tied_index

        return max_mip

    # TODO rename to intrinsic information?
    def find_maximal_state_under_complete_partition(
        self,
        direction: Direction,
        mechanism: tuple[int],
        purview: tuple[int],
        return_information: bool = False,
        repertoire_distance: str = None,
        states: Iterable[Iterable[int]] = None,
        virtual_units: Iterable[int] = None,
    ):
        repertoire_distance = fallback(
            repertoire_distance, config.REPERTOIRE_DISTANCE_INFORMATION
        )
        if states is None:
            states = utils.all_states(len(purview))

        # Default to not virtualizing mechanism units
        if virtual_units is None:
            nonvirtualized_units = mechanism
        else:
            nonvirtualized_units = frozenset(self.node_indices) - frozenset(
                virtual_units
            )

        repertoire = self.repertoire(direction, mechanism, purview)
        partition = complete_partition(mechanism, purview)

        def evaluate_state(state):
            information, _ = self.evaluate_partition(
                direction,
                mechanism,
                purview,
                partition,
                partitioned_repertoire_kwargs=dict(
                    nonvirtualized_units=nonvirtualized_units,
                ),
                repertoire=repertoire,
                repertoire_distance=repertoire_distance,
                state=state,
            )
            return information

        state_to_information = {state: evaluate_state(state) for state in states}
        max_information = max(state_to_information.values())
        # Return all tied states
        tied_states = [
            state
            for state, information in state_to_information.items()
            if information == max_information
        ]
        if return_information:
            return tied_states, max_information
        return tied_states

    # Phi_max methods
    # =========================================================================

    def potential_purviews(self, direction, mechanism, purviews=False):
        """Return all purviews that could belong to the |MIC|/|MIE|.

        Filters out trivially-reducible purviews.

        Args:
            direction (Direction): |CAUSE| or |EFFECT|.
            mechanism (tuple[int]): The mechanism of interest.

        Keyword Args:
            purviews (tuple[int]): Optional subset of purviews of interest.
        """
        # TODO(4.0) return set from network.potential_purviews?
        _potential_purviews = set(self.network.potential_purviews(direction, mechanism))
        if purviews is False:
            purviews = _potential_purviews
        else:
            # Restrict to given purviews
            purviews = _potential_purviews & set(purviews)
        # Restrict to purviews within the subsystem
        purviews = [
            purview for purview in purviews if set(purview).issubset(self.node_indices)
        ]
        # Purviews are already filtered in network.potential_purviews
        # over the full network connectivity matrix. However, since the cm
        # is cut/smaller we check again here.
        return irreducible_purviews(self.cm, direction, mechanism, purviews)

    @cache.method("_mice_cache")
    def find_mice(self, direction, mechanism, purviews=False):
        """Return the |MIC| or |MIE| for a mechanism.

        Args:
            direction (Direction): :|CAUSE| or |EFFECT|.
            mechanism (tuple[int]): The mechanism to be tested for
                irreducibility.

        Keyword Args:
            purviews (tuple[int]): Optionally restrict the possible purviews
                to a subset of the subsystem. This may be useful for _e.g._
                finding only concepts that are "about" a certain subset of
                nodes.

        Returns:
            MaximallyIrreducibleCauseOrEffect: The |MIC| or |MIE|.
        """
        purviews = self.potential_purviews(direction, mechanism, purviews)

        if direction == Direction.CAUSE:
            mice_class = MaximallyIrreducibleCause
        elif direction == Direction.EFFECT:
            mice_class = MaximallyIrreducibleEffect
        else:
            validate.direction(direction)

        if not purviews:
            max_mice = mice_class(_null_ria(direction, mechanism, ()), ties=())
        else:
            if config.IIT_VERSION == 4:
                # TODO(4.0)
                all_mips = [
                    self.find_maximally_irreducible_state(direction, mechanism, purview)
                    for purview in purviews
                ]
            elif config.IIT_VERSION == "maximal-state-first":
                # TODO(4.0) keep track of MIPs with respect to all tied states:
                # confirm that current strategy of simply including all ties in
                # `all_mips` and taking max is correct
                all_mips = []
                for purview in purviews:
                    maximal_states = self.find_maximal_state_under_complete_partition(
                        direction, mechanism, purview
                    )
                    mips = [
                        self.find_mip(direction, mechanism, purview, state=state)
                        for state in maximal_states
                    ]
                    maximal_states = np.array(maximal_states)
                    for mip in mips:
                        mip.set_specified_state(maximal_states)
                    all_mips.extend(mips)
            else:
                all_mips = [
                    self.find_mip(direction, mechanism, purview) for purview in purviews
                ]

            ties = resolve_ties.mice(list(map(mice_class, all_mips)))
            for tie in ties:
                tie.set_ties(ties)
            max_mice = ties[0]

        return max_mice

    def mic(self, mechanism, purviews=False):
        """Return the mechanism's maximally-irreducible cause (|MIC|).

        Alias for |find_mice()| with ``direction`` set to |CAUSE|.
        """
        return self.find_mice(Direction.CAUSE, mechanism, purviews=purviews)

    def mie(self, mechanism, purviews=False):
        """Return the mechanism's maximally-irreducible effect (|MIE|).

        Alias for |find_mice()| with ``direction`` set to |EFFECT|.
        """
        return self.find_mice(Direction.EFFECT, mechanism, purviews=purviews)

    def phi_max(self, mechanism):
        """Return the |small_phi_max| of a mechanism.

        This is the maximum of |small_phi| taken over all possible purviews.
        """
        return min(self.mic(mechanism).phi, self.mie(mechanism).phi)

    # Big Phi methods
    # =========================================================================

    @property
    def null_concept(self):
        """Return the null concept of this subsystem.

        The null concept is a point in concept space identified with
        the unconstrained cause and effect repertoire of this subsystem.
        """
        # Unconstrained cause repertoire.
        cause_repertoire = self.cause_repertoire((), ())
        # Unconstrained effect repertoire.
        effect_repertoire = self.effect_repertoire((), ())

        # Null cause.
        cause = MaximallyIrreducibleCause(
            _null_ria(Direction.CAUSE, (), (), cause_repertoire)
        )
        # Null effect.
        effect = MaximallyIrreducibleEffect(
            _null_ria(Direction.EFFECT, (), (), effect_repertoire)
        )

        # All together now...
        return Concept(mechanism=(), cause=cause, effect=effect, subsystem=self)

    def concept(
        self, mechanism, purviews=False, cause_purviews=False, effect_purviews=False
    ):
        """Return the concept specified by a mechanism within this subsytem.

        Args:
            mechanism (tuple[int]): The candidate set of nodes.

        Keyword Args:
            purviews (tuple[tuple[int]]): Restrict the possible purviews to
                those in this list.
            cause_purviews (tuple[tuple[int]]): Restrict the possible cause
                purviews to those in this list. Takes precedence over
                ``purviews``.
            effect_purviews (tuple[tuple[int]]): Restrict the possible effect
                purviews to those in this list. Takes precedence over
                ``purviews``.

        Returns:
            Concept: The pair of maximally irreducible cause/effect repertoires
            that constitute the concept specified by the given mechanism.
        """
        log.debug("Computing concept %s...", mechanism)

        # If the mechanism is empty, there is no concept.
        if not mechanism:
            log.debug("Empty concept; returning null concept")
            return self.null_concept

        # Calculate the maximally irreducible cause repertoire.
        cause = self.mic(mechanism, purviews=(cause_purviews or purviews))

        # Calculate the maximally irreducible effect repertoire.
        effect = self.mie(mechanism, purviews=(effect_purviews or purviews))

        log.debug("Found concept %s", mechanism)

        # NOTE: Make sure to expand the repertoires to the size of the
        # subsystem when calculating concept distance. For now, they must
        # remain un-expanded so the concept doesn't depend on the subsystem.
        return Concept(mechanism=mechanism, cause=cause, effect=effect, subsystem=self)

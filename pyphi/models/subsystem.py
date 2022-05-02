#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# models/subsystem.py

"""Subsystem-level objects."""

from collections import defaultdict
from collections.abc import Sequence, Iterable

from toolz import concat, merge

from pyphi.direction import Direction

from .. import utils
from . import cmp, fmt
from .mechanism import Concept

_sia_attributes = ["phi", "ces", "partitioned_ces", "subsystem", "cut_subsystem"]


def _concept_sort_key(concept):
    return (len(concept.mechanism), concept.mechanism)


class CauseEffectStructure(cmp.Orderable, Sequence):
    """A collection of concepts."""

    def __init__(self, concepts=(), subsystem=None):
        # Normalize the order of concepts
        # TODO(4.0) convert to set?
        self.concepts = tuple(sorted(concepts, key=_concept_sort_key))
        self.subsystem = subsystem
        self._specifiers = None
        self._purview_inclusion = defaultdict(set)
        self._purview_inclusion_max_order = 0
        self._purview_inclusion_by_order = defaultdict(set)

    def __len__(self):
        return len(self.concepts)

    def __iter__(self):
        return iter(self.concepts)

    def __getitem__(self, i):
        return self.concepts[i]

    def __repr__(self):
        # TODO(4.0) remove dependence on subsystem & time
        return fmt.make_repr(self, ["concepts", "subsystem"])

    def __str__(self):
        return fmt.fmt_ces(self)

    @cmp.sametype
    def __eq__(self, other):
        return self.concepts == other.concepts

    def __hash__(self):
        return hash((self.concepts, self.subsystem))

    def order_by(self):
        return [self.concepts]

    def to_json(self):
        return {
            "concepts": self.concepts,
            "subsystem": self.subsystem,
        }

    def flatten(self):
        """Return this as a FlatCauseEffectStructure."""
        return FlatCauseEffectStructure(self)

    @property
    def phis(self):
        """The |small_phi| values of each concept."""
        for concept in self:
            yield concept.phi

    @property
    def mechanisms(self):
        """The mechanism of each concept."""
        for concept in self:
            yield concept.mechanism

    def _purviews(self, direction):
        for concept in self:
            yield concept.purview(direction)

    def purviews(self, direction):
        """Return the purview of each concept in the given direction."""
        if isinstance(direction, Iterable):
            for _direction in direction:
                yield from self._purviews(_direction)
        else:
            yield self._purview(direction)

    @property
    def labeled_mechanisms(self):
        """The labeled mechanism of each concept."""
        # TODO(4.0) remove dependence on subsystem
        label = self.subsystem.node_labels.indices2labels
        return tuple(list(label(mechanism)) for mechanism in self.mechanisms)

    def purview_inclusion(self, max_order=None):
        if max_order is None:
            max_order = len(self.subsystem)
        max_order = min(len(self.subsystem), max_order)
        if max_order > self._purview_inclusion_max_order:
            self.flatten().compute_purview_inclusion(max_order)
        self._purview_inclusion_max_order = max_order
        if max_order < len(self.subsystem):
            # Return only the inclusion structure up to the max order
            return merge(
                *(
                    {
                        key: self._purview_inclusion[key]
                        for key in self._purview_inclusion_by_order[i]
                    }
                    for i in range(1, max_order + 1)
                )
            )
        else:
            return self._purview_inclusion


class FlatCauseEffectStructure(CauseEffectStructure):
    """A collection of maximally-irreducible components in either causal
    direction."""

    def __init__(self, concepts=(), subsystem=None):
        if isinstance(concepts, CauseEffectStructure):
            subsystem = concepts.subsystem
        if not isinstance(concepts, FlatCauseEffectStructure):
            _concepts = concat(
                [concept.cause, concept.effect]
                if isinstance(concept, Concept)
                else [concept]
                for concept in concepts
            )
        else:
            _concepts = iter(concepts)
        super().__init__(concepts=_concepts, subsystem=subsystem)
        try:
            # NOTE: Pointing to the same dictionary is required here, so that
            # calling `compute_purview_inclusion` on a flattened CES will update
            # the unflattened CES's properties
            self._purview_inclusion = concepts._purview_inclusion
            self._purview_inclusion_max_order = concepts._purview_inclusion_max_order
            self._purview_inclusion_by_order = concepts._purview_inclusion_by_order
        except AttributeError:
            pass

    def __str__(self):
        return fmt.fmt_ces(self, title="Flat cause-effect structure")

    @property
    def purviews(self):
        """The purview of each component."""
        for component in self:
            yield component.purview

    @property
    def specified_purviews(self):
        """The set of unique purviews specified by this CES."""
        return set(self.purviews)

    def specifiers(self, purview):
        """The components that specify the given purview."""
        purview = tuple(purview)
        try:
            return self._specifiers[purview]
        except TypeError:
            self._specifiers = defaultdict(list)
            for component in self:
                self._specifiers[component.purview].append(component)
            return self._specifiers[purview]

    def maximum_specifier(self, purview):
        """Return the components that maximally specify the given purview."""
        return max(self.specifiers(purview))

    def maximum_specifiers(self):
        """Return a mapping from each purview to its maximum specifier."""
        return {purview: self.maximum_specifier(purview) for purview in self.purviews}

    def flatten(self):
        return self

    def unflatten(self):
        mechanism_to_mice = defaultdict(dict)
        for mice in self:
            mechanism_to_mice[mice.mechanism][mice.direction] = mice
        return CauseEffectStructure(
            [
                Concept(
                    mechanism=mechanism,
                    cause=mice[Direction.CAUSE],
                    effect=mice[Direction.EFFECT],
                    subsystem=self.subsystem,
                )
                for mechanism, mice in mechanism_to_mice.items()
            ],
            subsystem=self.subsystem,
        )

    def compute_purview_inclusion(self, max_order):
        for distinction in self:
            for subset in utils.powerset(
                distinction.purview,
                nonempty=True,
                min_size=(self._purview_inclusion_max_order + 1),
                max_size=max_order,
            ):
                # NOTE: This considers "includes" to mean "congruent
                # with any of the tied states"
                substates = utils.specified_substate(
                    distinction.purview, distinction.specified_state, subset
                )
                for substate in map(tuple, substates):
                    key = (subset, substate)
                    self._purview_inclusion[key].add(distinction)
                    self._purview_inclusion_by_order[len(subset)].add(key)


class SystemIrreducibilityAnalysis(cmp.Orderable):
    """An analysis of system irreducibility (|big_phi|).

    Contains the |big_phi| value of the |Subsystem|, the cause-effect
    structure, and all the intermediate results obtained in the course of
    computing them.

    These can be compared with the built-in Python comparison operators (``<``,
    ``>``, etc.). First, |big_phi| values are compared. Then, if these are
    equal up to |PRECISION|, the one with the larger subsystem is greater.

    Attributes:
        phi (float): The |big_phi| value for the subsystem when taken against
            this analysis, *i.e.* the difference between the cause-effect
            structure and the partitioned cause-effect structure for this
            analysis.
        ces (CauseEffectStructure): The cause-effect structure of
            the whole subsystem.
        partitioned_ces (CauseEffectStructure): The cause-effect structure when
            the subsystem is cut.
        subsystem (Subsystem): The subsystem this analysis was calculated for.
        cut_subsystem (Subsystem): The subsystem with the minimal cut applied.
        time (float): The number of seconds it took to calculate.
    """

    def __init__(
        self,
        phi=None,
        ces=None,
        partitioned_ces=None,
        subsystem=None,
        cut_subsystem=None,
    ):
        self.phi = phi
        self.ces = ces
        self.partitioned_ces = partitioned_ces
        self.subsystem = subsystem
        self.cut_subsystem = cut_subsystem

    def __repr__(self):
        return fmt.make_repr(self, _sia_attributes)

    def __str__(self, ces=True):
        return fmt.fmt_sia(self, ces=ces)

    def print(self, ces=True):
        """Print this |SystemIrreducibilityAnalysis|, optionally without
        cause-effect structures.
        """
        print(self.__str__(ces=ces))

    @property
    def cut(self):
        """The unidirectional cut that makes the least difference to the
        subsystem.
        """
        return self.cut_subsystem.cut

    @property
    def network(self):
        """The network the subsystem belongs to."""
        return self.subsystem.network

    unorderable_unless_eq = ["network"]

    def order_by(self):
        return [self.phi, len(self.subsystem), self.subsystem.node_indices]

    def __eq__(self, other):
        return cmp.general_eq(self, other, _sia_attributes)

    def __bool__(self):
        """A |SystemIrreducibilityAnalysis| is ``True`` if it has
        |big_phi > 0|.
        """
        return not utils.eq(self.phi, 0)

    def __hash__(self):
        return hash(
            (
                self.phi,
                self.ces,
                self.partitioned_ces,
                self.subsystem,
                self.cut_subsystem,
            )
        )

    def to_json(self):
        """Return a JSON-serializable representation."""
        return {
            attr: getattr(self, attr) for attr in _sia_attributes + ["small_phi_time"]
        }

    @classmethod
    def from_json(cls, dct):
        del dct["small_phi_time"]
        return cls(**dct)


def _null_ces(subsystem):
    """Return an empty CES."""
    return CauseEffectStructure((), subsystem=subsystem)


def _null_sia(subsystem, phi=0.0):
    """Return a |SystemIrreducibilityAnalysis| with zero |big_phi| and empty
    cause-effect structures.

    This is the analysis result for a reducible subsystem.
    """
    return SystemIrreducibilityAnalysis(
        subsystem=subsystem,
        cut_subsystem=subsystem,
        phi=phi,
        ces=_null_ces(subsystem),
        partitioned_ces=_null_ces(subsystem),
    )

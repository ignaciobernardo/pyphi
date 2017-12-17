import pickle

import numpy as np
import pytest

from pyphi import Direction, compute, config
from pyphi.compute import (ConceptStyleSystem,
                           SystemIrreducibilityAnalysisConceptStyle,
                           concept_cuts)
from pyphi.models import KCut, KPartition, Part
from test_models import bigmip


@pytest.fixture()
def kcut_past():
    partition = KPartition(
        Part((0, 2), (0,)), Part((), (2,)), Part((3,), (3,)))
    return KCut(Direction.CAUSE, partition)


@pytest.fixture()
def kcut_future():
    partition = KPartition(
        Part((0, 2), (0,)), Part((), (2,)), Part((3,), (3,)))
    return KCut(Direction.EFFECT, partition)


def test_cut_indices(kcut_past, kcut_future):
    assert kcut_past.indices == (0, 2, 3)
    assert kcut_future.indices == (0, 2, 3)


def test_apply_cut(kcut_past, kcut_future):
    cm = np.ones((4, 4))
    cut_cm = np.array([
        [1, 1, 1, 0],
        [1, 1, 1, 1],
        [0, 1, 0, 0],
        [0, 1, 0, 1]])
    assert np.array_equal(kcut_past.apply_cut(cm), cut_cm)

    cm = np.ones((4, 4))
    cut_cm = np.array([
        [1, 1, 0, 0],
        [1, 1, 1, 1],
        [1, 1, 0, 0],
        [0, 1, 0, 1]])
    assert np.array_equal(kcut_future.apply_cut(cm), cut_cm)


def test_cut_matrix(kcut_past, kcut_future):
    assert np.array_equal(kcut_past.cut_matrix(4), np.array([
        [0, 0, 0, 1],
        [0, 0, 0, 0],
        [1, 0, 1, 1],
        [1, 0, 1, 0]]))

    assert np.array_equal(kcut_future.cut_matrix(4), np.array([
        [0, 0, 1, 1],
        [0, 0, 0, 0],
        [0, 0, 1, 1],
        [1, 0, 1, 0]]))


def test_splits_mechanism(kcut_past):
    assert kcut_past.splits_mechanism((0, 3))
    assert kcut_past.splits_mechanism((2, 3))
    assert not kcut_past.splits_mechanism((0,))
    assert not kcut_past.splits_mechanism((3,))


def test_all_cut_mechanisms(kcut_past):
    assert kcut_past.all_cut_mechanisms() == (
        (2,), (0, 2), (0, 3), (2, 3), (0, 2, 3))


@config.override(PARTITION_TYPE='TRI')
def test_concept_style_cuts():
    assert list(concept_cuts(Direction.CAUSE, (0,))) == [
        KCut(Direction.CAUSE, KPartition(
            Part((), ()), Part((), (0,)), Part((0,), ())))]
    assert list(concept_cuts(Direction.EFFECT, (0,))) == [
        KCut(Direction.EFFECT, KPartition(
            Part((), ()), Part((), (0,)), Part((0,), ())))]


def test_kcut_equality(kcut_past, kcut_future):
    other = KCut(Direction.CAUSE, KPartition(
        Part((0, 2), (0,)), Part((), (2,)), Part((3,), (3,))))
    assert kcut_past == other
    assert hash(kcut_past) == hash(other)
    assert hash(kcut_past) != hash(kcut_past.partition)

    assert kcut_past != kcut_future
    assert hash(kcut_past) != hash(kcut_future)


def test_system_accessors(s):
    cut_past = KCut(Direction.CAUSE, KPartition(
        Part((0, 2), (0, 1)), Part((1,), (2,))))
    cs_past = ConceptStyleSystem(s, Direction.CAUSE, cut_past)
    assert cs_past.cause_system.cut == cut_past
    assert not cs_past.effect_system.is_cut

    cut_future = KCut(Direction.EFFECT, KPartition(
        Part((0, 2), (0, 1)), Part((1,), (2,))))
    cs_future = ConceptStyleSystem(s, Direction.EFFECT, cut_future)
    assert not cs_future.cause_system.is_cut
    assert cs_future.effect_system.cut == cut_future


def sia_cs(phi=1.0, subsystem=None):
    return SystemIrreducibilityAnalysisConceptStyle(
        mip_past=bigmip(phi=phi, subsystem=subsystem),
        mip_future=bigmip(phi=phi, subsystem=subsystem))


def test_sia_concept_style_ordering(s, subsys_n0n2, s_noised):
    assert sia_cs(subsystem=s) == sia_cs(subsystem=s)
    assert sia_cs(phi=1, subsystem=s) < sia_cs(phi=2, subsystem=s)

    assert sia_cs(subsystem=s) >= sia_cs(subsystem=subsys_n0n2)

    with pytest.raises(TypeError):
        sia_cs(subsystem=s) < sia_cs(subsystem=s_noised)


def test_sia_concept_style(s):
    mip = compute.sia_concept_style(s)
    assert mip.min_mip is mip.sia_future
    for attr in ['phi', 'unpartitioned_ces', 'cut', 'subsystem',
                 'cut_subsystem', 'network', 'partitioned_ces']:
        assert getattr(mip, attr) is getattr(mip.sia_future, attr)


@config.override(SYSTEM_CUTS='CONCEPT_STYLE')
def test_unpickle(s, flushcache, restore_fs_cache):
    bm = compute.sia(s)
    pickle.loads(pickle.dumps(bm))


@config.override(SYSTEM_CUTS='CONCEPT_STYLE')
def test_concept_style_phi(s, flushcache, restore_fs_cache):
    assert compute.big_phi(s) == 0.6875

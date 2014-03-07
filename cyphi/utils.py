#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cyphi.utils
~~~~~~~~~~~

This module provides utility functions used within CyPhi that are consumed by
more than one class.

"""

import numpy as np
from re import match
from itertools import chain, combinations
from scipy.misc import comb
from .exceptions import ValidationException
from scipy.spatial.distance import cdist
from pyemd import emd as _emd


# see http://stackoverflow.com/questions/16003217
def combs(a, r):
    """
    NumPy implementation of itertools.combinations.

    Return successive :math:`r`-length combinations of elements in the array
    `a`.

    :param a: the array from which to get combinations
    :type a: ``np.ndarray``
    :param r:  the length of the combinations
    :type r: ``int``

    :returns: An array of combinations
    :rtype: ``np.ndarray``
    """
    # Special-case for 0-length combinations
    if r is 0:
        return np.asarray([])

    a = np.asarray(a)
    data_type = a.dtype if r is 0 else np.dtype([('', a.dtype)] * r)
    b = np.fromiter(combinations(a, r), data_type)
    return b.view(a.dtype).reshape(-1, r)


# see http://stackoverflow.com/questions/16003217/
def comb_indices(n, k):
    """
    N-D version of itertools.combinations.

    Return indices that yeild the :math:`r`-combinations of :math:`n` elements

        >>> n, k = 3, 2
        >>> data = np.arange(6).reshape(2, 3)
        >>> data[:, comb_indices(n, k)]
        array([[[0, 1],
                [0, 2],
                [1, 2]],
        <BLANKLINE>
               [[3, 4],
                [3, 5],
                [4, 5]]])

    :param a: array from which to get combinations
    :type a: ``np.ndarray``
    :param k: length of combinations
    :type k: ``int``

    :returns: Indices of the :math:`r`-combinations of :math:`n` elements
    :rtype: ``np.ndarray``
    """
    # Count the number of combinations for preallocation
    count = comb(n, k, exact=True)
    # Get numpy iterable from ``itertools.combinations``
    indices = np.fromiter(
        chain.from_iterable(combinations(range(n), k)),
        int,
        count=(count * k))
    # Reshape output into the array of combination indicies
    return indices.reshape(-1, k)


# TODO: implement this with numpy?
def powerset(iterable):
    """
    Return the power set of an iterable (see `itertools recipes
    <http://docs.python.org/2/library/itertools.html#recipes>`_).

        >>> ps = powerset(np.arange(2))
        >>> print(list(ps))
        [(), (0,), (1,), (0, 1)]

    :param iterable: The iterable from which to generate the power set
    :type iterable: iterable

    :returns: An iterator over the power set
    :rtype: iterator
    """
    s = list(iterable)
    return chain.from_iterable(combinations(s, r) for r in range(len(s) + 1))


def uniform_distribution(number_of_nodes):
    """
    Return the uniform distribution for a set of binary nodes, indexed by state
    (so there is one dimension per node, the size of which is the number of
    possible states for that node).

    :param nodes: a set of indices of binary nodes
    :type nodes: ``np.ndarray``

    :returns: The uniform distribution over the set of nodes
    :rtype: ``np.ndarray``
    """
    # The size of the state space for binary nodes is 2^(number of nodes).
    number_of_states = 2 ** number_of_nodes
    # Generate the maximum entropy distribution
    # TODO extend to nonbinary nodes
    return np.divide(np.ones(number_of_states),
                     number_of_states).reshape([2] * number_of_nodes)


def marginalize_out(node, tpm):
    """
    Marginalize out a node from a TPM.

    The TPM must be indexed by individual node state.

    :param node: The node to be marginalized out
    :type node: ``Node``
    :param tpm: The tpm to marginalize the node out of
    :type tpm: ``np.ndarray``

    :returns: The TPM after marginalizing out the node
    :rtype: ``np.ndarray``
    """
    # Preserve number of dimensions so node indices still index into the proper
    # axis of the returned distribution, normalize the distribution by number
    # of states
    return np.divide(np.sum(tpm, node.index, keepdims=True),
                     tpm.shape[node.index])


# TODO memoize this
def max_entropy_distribution(nodes, network):
    """
    Return the maximum entropy distribution over a set of nodes.

    This is different from the network's uniform distribution because nodes
    outside the are fixed and treated as if they have only 1 state.

    :param nodes: The set of nodes
    :type nodes: ``[Node]``
    :param network: The network the nodes belong to
    :type network: ``Network``

    :returns: The maximum entropy distribution over this subsystem
    :rtype: ``np.ndarray``
    """
    # TODO extend to nonbinary nodes
    max_ent_shape = [2 if node in nodes else 1 for node in network.nodes]
    return np.divide(np.ones(max_ent_shape),
                     np.ufunc.reduce(np.multiply, max_ent_shape))


# TODO extend to binary noes
# TODO parametrize and use other metrics? (KDL, L1)?
def emd(d1, d2):
    """Return the Earth Mover's Distance between two distributions (indexed
    by state, one dimension per node).

    Singleton dimensions are assumed to mean that probabilities do not
    depend on the corresponding node's state (so they are broadcast to make
    a full distribution over the whole state space).
    """
    # Expand the distributions to the whole state space, if needed
    if not (2 ** d1.ndim == sum(d1.shape)):
        full_d1 = np.ones([2] * d1.ndim)
        d1 = full_d1 * d1
    if not (2 ** d2.ndim == sum(d2.shape)):
        full_d2 = np.ones([2] * d2.ndim)
        d2 = full_d2 * d2
    # Compute the EMD with Hamming distance between states as the
    # transportation cost function
    return _emd(d1.ravel(), d2.ravel(), _hamming_matrix(d1.ndim))


# TODO [optimization] optimize this to use indices rather than nodes?
# TODO are native lists really slower?
def bipartition(a):
    """Generates all bipartitions for a list or array.

        >>> from cyphi.utils import bipartition
        >>> list(bipartition([1, 2, 3]))
        [([], [1, 2, 3]), ([1], [2, 3]), ([2], [1, 3]), ([1, 2], [3])]

    :param array: The list to partition
    :type array: ``[] or np.ndarray``

    :returns: A generator that yields a tuple containing each of the two
        partitions as numpy arrays
    :rtype: ``generator``
    """
    # Get size of list or array and validate
    if isinstance(a, type([])):
        size = len(a)
    elif isinstance(a, type(np.array([]))):
        size = a.size
    else:
        raise ValueError("Input must be a list or numpy array.")

    for bitstring in [bin(i)[2:].zfill(size)[::-1]
                      for i in range(2 ** (size - 1))]:
        yield (_bitstring_index(a, bitstring),
               _bitstring_index(a, _flip(bitstring)))


# Internal helper methods
# =======================


# TODO extend to nonbinary nodes
def _hamming_matrix(N):
    """Return a matrix of Hamming distances for the possible states of
    :math:`N` binary nodes.

        >>> from cyphi.utils import _hamming_matrix
        >>> _hamming_matrix(2)
        array([[ 0.,  1.,  1.,  2.],
               [ 1.,  0.,  2.,  1.],
               [ 1.,  2.,  0.,  1.],
               [ 2.,  1.,  1.,  0.]])

    :param N: The number of nodes under consideration
    :type N: ``int``

    :returns: A :math:`2^N \\times 2^N` where the
        :math:`ij^{\\textrm{th}}` element is the Hamming distance between state
        :math:`i` and state :math:`j`.
    :rtype: ``np.ndarray``
    """
    possible_states = np.array([list(bin(state)[2:].zfill(N)) for state in
                                range(2 ** N)])
    return cdist(possible_states, possible_states, 'hamming') * N


def _bitstring_index(a, bitstring):
    """Select elements of an list or array based on a binary string.

    The :math:`i^{\\textrm{th}}` element in the array is selected if there is a
    1 at the :math:`i^{\\textrm{th}}` position of the bitstring.

        >>> from cyphi.utils import _bitstring_index
        >>> bitstring = '10010100'
        >>> _bitstring_index([0, 1, 2, 3, 4, 5, 6, 7], bitstring)
        [0, 3, 5]

    :param a: The list or array to select from.
    :type a: ``[] or np.ndarray``
    :param bitstring: The binary string indicating which elements are to be
        selected.
    :type bitstring: ``str``

    :returns: An list of all the elements at indices where there is a 1 in the
        binary string
    :rtype: ``[]``

    """
    # Get size of list or array and validate
    if isinstance(a, type([])):
        size = len(a)
    elif isinstance(a, type(np.array([]))):
        size = a.size
        if a.ndim != 1:
            raise ValueError("Array must be 1-dimensional.")
    else:
        raise ValueError("Input must be a list or numpy array.")
    if size != len(bitstring):
        raise ValueError("The bitstring must be the same length as the array.")
    if not match('^[01]*$', bitstring):
        raise ValueError("Bitstring must contain only 1s and 0s. Did you " +
                         "forget to chop off the first two characters after " +
                         "using `bin`?")

    return [a[i] for i in range(size) if bitstring[i] == '1']


def _flip(bitstring):
    """Flip the bits in a string consisting of 1s and zeros."""
    return ''.join('1' if x == '0' else '0' for x in bitstring)


# TODO implement this?
def connectivity_matrix_to_tpm(connectivity_matrix):
    """
    :param connectivity_matrix: The network's connectivity matrix (must be
        square)
    :type connectivity_matrix: ``np.ndarray``
    :param tpm: The network's transition probability matrix (state-by-node
        form)
    :type tpm: ``np.ndarray``
    """
    # Ensure connectivity matrix is square
    if ((len(connectivity_matrix.shape) is not 2) or
            (connectivity_matrix.shape[0] is not
                connectivity_matrix.shape[1])):
        raise ValidationException("Connectivity matrix must be square.")

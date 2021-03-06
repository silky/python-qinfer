#!/usr/bin/python
# -*- coding: utf-8 -*-
##
# resamplers.py: Implementations of various resampling algorithms.
##
# © 2012 Chris Ferrie (csferrie@gmail.com) and
#        Christopher E. Granade (cgranade@gmail.com)
#     
# This file is a part of the Qinfer project.
# Licensed under the AGPL version 3.
##
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
##

## FEATURES ####################################################################

from __future__ import division

## ALL #########################################################################

# We use __all__ to restrict what globals are visible to external modules.
__all__ = [
    'LiuWestResampler'
]

## IMPORTS #####################################################################

import numpy as np
import scipy.linalg as la
import warnings

from utils import outer_product, particle_meanfn, particle_covariance_mtx

from qinfer import clustering
from qinfer._exceptions import ResamplerWarning

## CLASSES #####################################################################

class ClusteringResampler(object):
    r"""
    Creates a resampler that breaks the particles into clusters, then applies
    a secondary resampling algorithm to each cluster independently.
    
    :param secondary_resampler: Resampling algorithm to be applied to each
        cluster. If ``None``, defaults to ``LiuWestResampler()``.
    """
    
    def __init__(self, eps=0.5, secondary_resampler=None, min_particles=5, metric='euclidean', weighted=False, w_pow=0.5, quiet=True):
        self.secondary_resampler = (
            secondary_resampler
            if secondary_resampler is not None
            else LiuWestResampler()
        )
        
        self.eps = eps
        self.quiet = quiet
        self.min_particles = min_particles
        self.metric = metric
        self.weighted = weighted
        self.w_pow = w_pow
        
    ## METHODS ##
    
    def __call__(self, model, particle_weights, particle_locations):
        ## TODO: docstring.
        
        # Allocate new arrays to hold the weights and locations.        
        new_weights = np.empty(particle_weights.shape)
        new_locs    = np.empty(particle_locations.shape)
        
        # Loop over clusters, calling the secondary resampler for each.
        # The loop should include -1 if noise was found.
        for cluster_label, cluster_particles in clustering.particle_clusters(
                particle_locations, particle_weights,
                eps=self.eps, min_particles=self.min_particles, metric=self.metric,
                weighted=self.weighted, w_pow=self.w_pow,
                quiet=self.quiet
        ):
        
            # If we are resampling the NOISE label, we must use the global moments.
            if cluster_label == clustering.NOISE:
                extra_args = {
                    "precomputed_mean": particle_meanfn(particle_weights, particle_locations, lambda x: x),
                    "precomputed_cov":  particle_covariance_mtx(particle_weights, particle_locations)
                }
            else:
                extra_args = {}
            
            # Pass the particles in that cluster to the secondary resampler
            # and record the new weights and locations.
            cluster_ws, cluster_locs = self.secondary_resampler(model,
                particle_weights[cluster_particles],
                particle_locations[cluster_particles],
                **extra_args
            )
            
            # Renormalize the weights of each resampled particle by the total
            # weight of the cluster to which it belongs.
            cluster_ws /= np.sum(particle_weights[cluster_particles])
            
            # Store the updated cluster.
            new_weights[cluster_particles] = cluster_ws
            new_locs[cluster_particles]    = cluster_locs

        # Assert that we have not introduced any NaNs or Infs by resampling.
        assert np.all(np.logical_not(np.logical_or(
                np.isnan(new_locs), np.isinf(new_locs)
            )))
            
        return new_weights, new_locs

class LiuWestResampler(object):
    r"""
    Creates a resampler instance that applies the algorithm of
    [LW01]_ to redistribute the particles.
    
    :param float a: Value of the parameter :math:`a` of the [LW01]_ algorithm
        to use in resampling.
    :param int maxiter: Maximum number of times to attempt to resample within
        the space of valid models before giving up.
    """
    def __init__(self, a=0.98, maxiter=1000):
        self.a = a # Implicitly calls the property setter below to set _h.
        self._maxiter = maxiter

    ## PROPERTIES ##

    @property
    def a(self):
        return self._a
        
    @a.setter
    def a(self, new_a):
        self._a = new_a
        self._h = np.sqrt(1 - new_a**2)

    ## METHODS ##
    
    def __call__(self, model, particle_weights, particle_locations, precomputed_mean=None, precomputed_cov=None):
        """
        Resample the particles according to algorithm given in 
        [LW01]_.
        """
        
        # Give shorter names to weights and locations.
        w, l = particle_weights, particle_locations
        
        # Possibly recompute moments, if not provided.
        if precomputed_mean is None:
            mean = particle_meanfn(w, l, lambda x: x)
        else:
            mean = precomputed_mean
        if precomputed_cov is None:
            cov = particle_covariance_mtx(w, l)
        else:
            cov = precomputed_cov
        
        # parameters in the Liu and West algorithm            
        a, h = self._a, self._h
        S, S_err = la.sqrtm(cov, disp=False)
    	S = np.real(h * S)
        n_ms, n_mp = l.shape
        
        new_locs = np.empty(l.shape)        
        cumsum_weights = np.cumsum(w)
        
        idxs_to_resample = np.arange(n_ms)
        
        # Preallocate js and mus so that we don't have rapid allocation and
        # deallocation.
        js = np.empty(idxs_to_resample.shape, dtype=int)
        mus = np.empty(l.shape, dtype=l.dtype)
        
        # Loop as long as there are any particles left to resample.
        n_iters = 0
        while idxs_to_resample.size and n_iters < self._maxiter:
            # Keep track of how many iterations we used.
            n_iters += 1
            
            # Draw j with probability self.particle_weights[j].
            # We do this by drawing random variates uniformly on the interval
            # [0, 1], then see where they belong in the CDF.
            js[:] = cumsum_weights.searchsorted(
                np.random.random((idxs_to_resample.size,)),
                side='right'
            )
            
            # Set mu_i to a x_j + (1 - a) mu.
            mus[...] = a * l[js,:] + (1 - a) * mean
            
            # Draw x_i from N(mu_i, S).
            new_locs[idxs_to_resample, :] = mus + np.dot(S, np.random.randn(n_mp, mus.shape[0])).T
            
            # Now we remove from the list any valid models.
            idxs_to_resample = idxs_to_resample[np.nonzero(np.logical_not(
                model.are_models_valid(new_locs[idxs_to_resample, :])
            ))[0]]

            # This may look a little weird, but it should delete the unused
            # elements of js, so that we don't need to reallocate.
            js = js[:idxs_to_resample.size]
            mus = mus[:idxs_to_resample.size, :]
            
        if idxs_to_resample.size:
            # We failed to force all models to be valid within maxiter attempts.
            # This means that we could be propagating out invalid models, and
            # so we should warn about that.
            warnings.warn((
                "Liu-West resampling failed to find valid models for {} "
                "particles within {} iterations."
            ).format(idxs_to_resample.size, self._maxiter), ResamplerWarning)

        # Now we reset the weights to be uniform, letting the density of
        # particles represent the information that used to be stored in the
        # weights. This is done by SMCUpdater, and so we simply need to return
        # the new locations here.
        return np.ones((w.shape[0],)) / w.shape[0], new_locs
        
    

'''
    Contains all the mathematical helper functions used throughout the scaffold.
    Differs from helpers.py only categorically. Helpers.py contains functions,
    classes and general logic that supports the scaffold, while functions.py
    contains a collection of mathematical functions.
'''

import numpy as np
import random
from scipy.spatial import distance

def compute_circle(center, radius, n_samples=50):
    '''
        Create `n_samples` points on a circle based on given `center` and `radius`.

        :param center: XYZ vector of the circle center
        :type center: array-like
        :param radius: Radius of the circle
        :type radius: scalar value
        :param n_samples: Amount of points on the circle.
        :type n_samples: int
    '''
    nodes = np.linspace(0,2*np.pi,n_samples, endpoint=False)
    x, y = np.sin(nodes)*radius+center[0], np.cos(nodes)*radius+center[1]
    return np.column_stack([x,y])

def apply_2d_bounds(possible_points, cell_bounds):
    '''
        Compare a 2xN matrix of XZ coordinates to a matrix 2x3 with a minimum column and maximum column of XYZ coordinates.
    '''
    x_mask = (possible_points[:,0].__ge__(cell_bounds[0,0])) & (possible_points[:,0].__le__(cell_bounds[0,1]))
    z_mask = (possible_points[:,1].__ge__(cell_bounds[2,0])) & (possible_points[:,1].__le__(cell_bounds[2,1]))
    return possible_points[x_mask & z_mask]

def get_candidate_points(center, radius, bounds, min_ϵ, max_ϵ, return_ϵ=False):
    '''
        Returns a list of points that are suited next candidates in a random walk.

        Computes a circle of points between `2r + ϵ` distance away from the center
        and removes any points that lie outside of the given bounds.

        :param center: 2D position of the starting point.
        :type center: list
        :param radius: Unit distance radius of the particle at the center point.
        :type radius: float
        :param bounds: A 2x3 matrix where the first column are the minimum XYZ
            and the last column the maximum XYZ.
        :type bounds: ndarray
        :param min_ϵ: Lower bound of epsilon used to calculate random distance.
        :type min_ϵ: float
        :param max_ϵ: Upper bound of epsilon used to calculate random distance.
        :type max_ϵ: float
        :param return_ϵ: If `True` the candidates and ϵ used to calculate them
            will be returned as a tuple.
    '''
    # Determine the uniformly random ϵ
    rnd_ϵ = np.random.uniform(min_ϵ, max_ϵ)
    # Create a circle of points `2r + ϵ`
    possible_points = compute_circle(center, radius * 2 + rnd_ϵ)
    # Get only the candidates within the bounds
    candidates = apply_2d_bounds(possible_points, bounds)
    if return_ϵ:
        return candidates, rnd_ϵ
    return candidates

def exclude_index(arr, index):
    '''
        Return a new list with the element at `index` removed.
    '''
    return [arr[i] for i in range(len(arr)) if i != index]

def add_y_axis(points, min, max):
    '''
        Add random values to the 2nd column of a matrix of 2D points.
    '''
    return np.insert(points, 1, np.random.uniform(min, max, points.shape[0]), axis=1)

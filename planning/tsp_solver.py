from __future__ import annotations

import math
from dataclasses import dataclass


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class TSPSolution:
    order: list[int]
    distance_m: float


class TSPSolver:
    def solve(
        self,
        targets_xy: list[tuple[float, float]],
        start_xy: tuple[float, float] = (0.0, 0.0),
        include_return_to_start: bool = False,
    ) -> TSPSolution:
        if not targets_xy:
            return TSPSolution(order=[], distance_m=0.0)

        points = [start_xy] + targets_xy
        route = self._nearest_neighbor(points)
        route = self._two_opt(route, points)

        if include_return_to_start and route[-1] != 0:
            route.append(0)

        ordered_target_indices = [idx - 1 for idx in route if idx != 0]
        total_distance = self._route_distance(route, points)
        return TSPSolution(order=ordered_target_indices, distance_m=total_distance)

    def _nearest_neighbor(self, points: list[tuple[float, float]]) -> list[int]:
        unvisited = set(range(1, len(points)))
        route = [0]

        while unvisited:
            last = route[-1]
            next_idx = min(unvisited, key=lambda idx: _distance(points[last], points[idx]))
            route.append(next_idx)
            unvisited.remove(next_idx)

        return route

    def _two_opt(self, route: list[int], points: list[tuple[float, float]]) -> list[int]:
        if len(route) < 4:
            return route

        improved = True
        best_route = route[:]
        best_distance = self._route_distance(best_route, points)

        while improved:
            improved = False
            for i in range(1, len(best_route) - 2):
                for j in range(i + 1, len(best_route) - 1):
                    candidate = best_route[:]
                    candidate[i : j + 1] = reversed(candidate[i : j + 1])
                    candidate_distance = self._route_distance(candidate, points)
                    if candidate_distance + 1e-6 < best_distance:
                        best_route = candidate
                        best_distance = candidate_distance
                        improved = True
            route = best_route
        return route

    def _route_distance(self, route: list[int], points: list[tuple[float, float]]) -> float:
        if len(route) < 2:
            return 0.0
        total = 0.0
        for index in range(1, len(route)):
            total += _distance(points[route[index - 1]], points[route[index]])
        return total

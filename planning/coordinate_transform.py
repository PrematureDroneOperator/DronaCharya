import math
from typing import Tuple


class CoordinateTransformer:
    def __init__(self, meters_per_pixel: float) -> None:
        if meters_per_pixel <= 0:
            raise ValueError("meters_per_pixel must be > 0.")
        self.meters_per_pixel = meters_per_pixel

    def pixel_to_relative(
        self, pixel_x: float, pixel_y: float, image_width: int, image_height: int
    ) -> Tuple[float, float]:
        center_x = image_width / 2.0
        center_y = image_height / 2.0

        relative_x = (pixel_x - center_x) * self.meters_per_pixel
        relative_y = (center_y - pixel_y) * self.meters_per_pixel
        return relative_x, relative_y

    def relative_to_gps(
        self, start_latitude: float, start_longitude: float, relative_x_m: float, relative_y_m: float
    ) -> Tuple[float, float]:
        meters_per_degree_lat = 111_320.0
        meters_per_degree_lon = max(1e-6, meters_per_degree_lat * math.cos(math.radians(start_latitude)))

        delta_lat = relative_y_m / meters_per_degree_lat
        delta_lon = relative_x_m / meters_per_degree_lon

        return start_latitude + delta_lat, start_longitude + delta_lon

    def gps_to_relative(
        self, start_latitude: float, start_longitude: float, target_latitude: float, target_longitude: float
    ) -> Tuple[float, float]:
        meters_per_degree_lat = 111_320.0
        meters_per_degree_lon = max(1e-6, meters_per_degree_lat * math.cos(math.radians(start_latitude)))

        dy = (target_latitude - start_latitude) * meters_per_degree_lat
        dx = (target_longitude - start_longitude) * meters_per_degree_lon
        return dx, dy

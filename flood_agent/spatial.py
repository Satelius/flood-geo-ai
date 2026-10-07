import math


def line_length_m(coordinates):
    total = 0
    for left, right in zip(coordinates, coordinates[1:]):
        lon1, lat1, lon2, lat2 = map(math.radians, [*left, *right])
        value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        total += 6371000 * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0, 1 - value)))
    return round(total, 1)


def lines_intersect(first, second):
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def touches(a, b, point):
        return abs(cross(a, b, point)) < 1e-12 and all(min(a[index], b[index]) - 1e-12 <= point[index] <= max(a[index], b[index]) + 1e-12 for index in (0, 1))

    for a, b in zip(first, first[1:]):
        for c, d in zip(second, second[1:]):
            if cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0:
                return True
            if touches(a, b, c) or touches(a, b, d) or touches(c, d, a) or touches(c, d, b):
                return True
    return False

from array import array
import math
from .values import scalar


def edge(a, b, x, y):
    return (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])


def rasterize(plan, checkpoint):
    width, height = plan["width"], plan["height"]
    length = width * height
    depth = array("d", [math.inf]) * length
    ids = array("I", [0]) * length
    colors = array("f", [0.]) * (length * 3)
    for pixel in range(length):
        for channel in range(3):
            colors[pixel * 3 + channel] = plan["background"][channel]
    fragments = 0
    for index, triangle in enumerate(plan["triangles"]):
        checkpoint()
        a, b, c = triangle["vertices"]
        area = edge(a, b, c[0], c[1])
        if abs(area) < 1e-10:
            continue
        left = max(0, math.floor(min(a[0], b[0], c[0])))
        right = min(width-1, math.ceil(max(a[0], b[0], c[0])))
        top = max(0, math.floor(min(a[1], b[1], c[1])))
        bottom = min(height-1, math.ceil(max(a[1], b[1], c[1])))
        for y in range(top, bottom + 1):
            if y % 32 == 0:
                checkpoint()
            for x in range(left, right + 1):
                wa = edge(b, c, x + .5, y + .5) / area
                wb = edge(c, a, x + .5, y + .5) / area
                wc = 1 - wa - wb
                if min(wa, wb, wc) < -1e-10:
                    continue
                if plan["projection"] == "perspective":
                    distance = 1 / (wa/a[2] + wb/b[2] + wc/c[2])
                else:
                    distance = wa*a[2] + wb*b[2] + wc*c[2]
                offset = y * width + x
                previous = depth[offset]
                tie = abs(distance - previous) < 1e-9
                if distance > previous + 1e-9 or (tie and triangle["object_id"] >= ids[offset]):
                    continue
                depth[offset] = distance
                ids[offset] = triangle["object_id"]
                colors[offset*3:offset*3+3] = array("f", triangle["color"])
                fragments += 1
    checkpoint()
    return {"depth": depth, "ids": ids, "colors": colors,
            "fragments": fragments, "triangles": len(plan["triangles"])}

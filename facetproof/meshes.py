from .algebra import bounds, cross, norm, sub
from .errors import require
from .values import identity, integer, text, vector, object_value


def validate_mesh(data):
    object_value(data)
    name = text(data.get("name"), "name")
    vertices = data.get("vertices")
    faces = data.get("faces")
    require(isinstance(vertices, list) and 3 <= len(vertices) <= 10000,
            "vertices", "Mesh needs 3..10000 vertices")
    vertices = [vector(p, "vertex", low=-10000, high=10000) for p in vertices]
    require(isinstance(faces, list) and 1 <= len(faces) <= 20000,
            "faces", "Mesh needs 1..20000 triangular faces")
    clean = []
    for face in faces:
        require(isinstance(face, list) and len(face) == 3,
                "face", "Faces must be index triples")
        indices = [integer(i, "index", 0, len(vertices)-1) for i in face]
        require(len(set(indices)) == 3, "face", "Repeated face vertex")
        a, b, c = [vertices[i] for i in indices]
        require(norm(cross(sub(b, a), sub(c, a))) > 1e-9,
                "degenerate", "Mesh contains a degenerate triangle")
        clean.append(indices)
    return {"name": name, "vertices": vertices, "faces": clean, "bounds": bounds(vertices)}


class Meshes:
    def __init__(self, db):
        self.db = db

    def create(self, data):
        payload = validate_mesh(data)
        with self.db.unit_of_work() as conn:
            return self.db.insert(conn, identity("mesh"), "mesh", "usable", payload)

    def retire(self, mesh_id, revision):
        with self.db.unit_of_work() as conn:
            mesh = self.db.get(conn, mesh_id, "mesh")
            require(mesh["state"] == "usable", "state", "Mesh already retired", 409)
            return self.db.update(conn, mesh, mesh["payload"], "retired", revision)

    def get(self, mesh_id):
        with self.db.unit_of_work() as conn:
            return self.db.get(conn, mesh_id, "mesh")

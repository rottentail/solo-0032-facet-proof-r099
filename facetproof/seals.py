from .errors import require
from .values import digest, identity, integer


class Seals:
    def __init__(self, db):
        self.db = db

    def create(self, assembly_id, revision):
        integer(revision, "revision", 1, 2**63-2)
        with self.db.unit_of_work() as conn:
            obj = self.db.get(conn, assembly_id, "assembly")
            require(obj["state"] == "draft", "state", "Assembly already sealed", 409)
            require(obj["revision"] == revision, "stale_revision", "Assembly changed", 409)
            visible = [p for p in obj["payload"]["placements"] if p["visible"]]
            require(bool(visible), "empty", "Seal requires visible geometry")
            closure = []
            for placement in visible:
                mesh = self.db.get(conn, placement["mesh_id"], "mesh")
                pigment = self.db.get(conn, placement["pigment_id"], "pigment")
                require(mesh["state"] == "usable", "retired", "Geometry retired before seal", 409)
                closure.append({"placement": placement, "mesh": mesh["payload"],
                                "pigment": pigment["payload"],
                                "mesh_revision": mesh["revision"], "pigment_revision": pigment["revision"]})
            content = {"assembly_id": assembly_id, "assembly_revision": revision,
                       "name": obj["payload"]["name"], "closure": closure,
                       "lens": obj["payload"]["lens"], "illumination": obj["payload"]["illumination"],
                       "format": "facet-seal-1"}
            seal = self.db.insert(conn, identity("seal"), "seal", "frozen",
                                  {**content, "content_hash": digest(content)})
            self.db.update(conn, obj, obj["payload"], "sealed", revision)
            self.db.event(conn, assembly_id, "sealed", {"seal_id": seal["id"]})
            return seal

    def get(self, seal_id):
        with self.db.unit_of_work() as conn:
            return self.db.get(conn, seal_id, "seal")

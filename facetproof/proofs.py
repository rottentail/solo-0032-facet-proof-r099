import json
from .errors import require
from .values import identity, text, canonical, now, choice


class Proofs:
    def __init__(self, db, plates):
        self.db, self.plates = db, plates

    def open(self, plate_id, data):
        note = text(data.get("note"), "note", 1000)
        with self.db.unit_of_work() as conn:
            plate = self.plates._get(conn, plate_id)
            proof_id = identity("proof")
            payload = {"note": note, "visual_hash": plate["payload"]["visual_hash"],
                       "seal_id": plate["payload"]["seal_id"]}
            conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?)",
                         (proof_id, plate_id, "open", canonical(payload), now()))
            self.db.event(conn, proof_id, "opened", {"plate_id": plate_id})
            return self._get(conn, proof_id)

    def _get(self, conn, proof_id):
        row = conn.execute("SELECT * FROM decisions WHERE id=?", (proof_id,)).fetchone()
        require(row is not None, "not_found", "Proof not found", 404)
        return {**dict(row), "payload": json.loads(row["payload"])}

    def get(self, proof_id):
        with self.db.unit_of_work() as conn:
            return self._get(conn, proof_id)

    def decide(self, proof_id, data):
        state = choice(data.get("state"), "state", {"accepted", "rejected"})
        note = text(data.get("note"), "note", 1000)
        with self.db.unit_of_work() as conn:
            proof = self._get(conn, proof_id)
            require(proof["state"] == "open", "state", "Proof already decided", 409)
            plate = self.plates._get(conn, proof["plate_id"])
            if state == "accepted":
                self.plates.verify_payload(plate["payload"])
            payload = {**proof["payload"], "decision_note": note}
            conn.execute("UPDATE decisions SET state=?,payload=? WHERE id=?", (state, canonical(payload), proof_id))
            self.db.event(conn, proof_id, state, {"plate_id": plate["id"]})
            return self._get(conn, proof_id)

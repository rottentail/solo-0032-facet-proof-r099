import argparse
import json
from pathlib import Path
import tempfile
from .application import Application
from .errors import Fault, require
from .routing import route_request, PREFIX

FLOWS = ["mesh-intake", "mesh-retirement", "assembly-edit", "lens-edit", "seal-closure", "exposure-submit", "render-plates", "cancel-exposure", "retry-exposure", "verify-plate", "pixel-pick", "compare-plates", "proof-decision", "quarantine-plate", "recover-lease", "export-bundle"]

def commission(root, flow, output=None):
    app = Application(root)
    def call(method, route, data=None): return route_request(app, method, PREFIX + route, data)
    def rejected(method, route, data, code):
        try: call(method, route, data)
        except Fault as exc:
            require(exc.code == code, "commission", f"Expected {code}, got {exc.code}", 500)
            return exc.code
        raise Fault("commission", "Expected rejection", 500)
    mesh_data = {"name":"斜翼", "vertices":[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0],[0,0,1.2]], "faces":[[0,1,4],[1,2,4],[2,3,4],[3,0,4],[0,3,2],[0,2,1]]}
    mesh = call("POST", "meshes", mesh_data)
    pigment = call("POST", "pigments", {"name":"釉青", "color":[.12,.62,.61], "two_sided":True})
    assembly = call("POST", "assemblies", {"name":"斜翼结构说明图", "lens":{"eye":[3,2,4]}})
    assembly = call("PUT", "assemblies/"+assembly["id"], {"revision":1, "placements":[{"key":"wing", "mesh_id":mesh["id"], "pigment_id":pigment["id"], "transform":{"euler":[0,20,0]}}]})
    result = {"workflow":flow}
    if flow == "mesh-intake":
        result["rejected"] = rejected("POST", "meshes", {**mesh_data, "faces":[[0,0,1]]}, "face")
        return result
    if flow == "mesh-retirement":
        call("POST", "meshes/"+mesh["id"]+"/retire", {"revision":1})
        result["rejected"] = rejected("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2}, "retired")
        return result
    if flow in ("assembly-edit", "lens-edit"):
        changes = {"name":"更新说明图"} if flow == "assembly-edit" else {"lens":{"projection":"orthographic"}}
        edited = call("PUT", "assemblies/"+assembly["id"], {"revision":2, **changes})
        result.update(revision=edited["revision"], rejected=rejected("PUT", "assemblies/"+assembly["id"], {"revision":2, "name":"过期"}, "stale_revision"))
        return result
    seal = call("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2})
    if flow == "seal-closure":
        call("PUT", "pigments/"+pigment["id"], {"revision":1, "name":"改色", "color":[1,0,0]})
        require(call("GET", "seals/"+seal["id"]) == seal, "commission", "Seal changed", 500)
        result["rejected"] = rejected("PUT", "assemblies/"+assembly["id"], {"revision":3, "name":"改稿"}, "sealed")
        return result
    request = {"key":"commission", "width":96, "height":96, "samples":2}
    exposure = call("POST", "seals/"+seal["id"]+"/exposures", request)
    eroute = "exposures/"+exposure["id"]
    if flow == "exposure-submit":
        require(call("POST", "seals/"+seal["id"]+"/exposures", request)["id"] == exposure["id"], "commission", "Idempotency mismatch", 500)
        result["rejected"] = rejected("POST", "seals/"+seal["id"]+"/exposures", {**request, "width":128}, "key_conflict")
        return result
    if flow == "cancel-exposure":
        result["state"] = call("POST", eroute+"/cancel")["state"]
        require(call("POST", "worker/step")["state"] == "idle", "commission", "Cancelled work claimed", 500)
        result["rejected"] = rejected("POST", eroute+"/retry", {}, "state")
        return result
    if flow in ("retry-exposure", "recover-lease"):
        claimed = app.exposures.claim(lease_seconds=-1 if flow == "recover-lease" else 120)
        if flow == "retry-exposure":
            app.exposures.fail(exposure["id"], claimed["token"], "interrupted")
            call("POST", eroute+"/retry")
        else:
            require(exposure["id"] in call("POST", "worker/recover")["recovered"], "commission", "Recovery failed", 500)
    rendered = call("POST", "worker/step")
    require(rendered["state"] == "succeeded", "commission", str(rendered), 500)
    plate = rendered["plate"]
    route = "plates/"+plate["id"]
    require(plate["payload"]["statistics"]["covered_pixels"] > 0, "commission", "Empty image", 500)
    result["statistics"] = plate["payload"]["statistics"]
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        for name in ("color.png", "depth.png", "object.png"): (Path(output)/name).write_bytes(call("GET", route+"/passes/"+name)[0])
    if flow == "verify-plate": result["verification"] = call("POST", route+"/verify")
    elif flow == "pixel-pick":
        result["pick"] = call("GET", route+"/pick?x=48&y=48")
        require(result["pick"]["object"] is not None, "commission", "Center object missing", 500)
    elif flow == "compare-plates":
        call("POST", "seals/"+seal["id"]+"/exposures", {**request, "key":"second"})
        second = call("POST", "worker/step")["plate"]
        result["comparison"] = call("POST", "comparisons", {"left":plate["id"], "right":second["id"]})["payload"]
        require(result["comparison"]["color_changed"] == 0, "commission", "Determinism failed", 500)
    elif flow in ("proof-decision", "quarantine-plate"):
        proof = call("POST", route+"/proofs", {"note":"检查结构轮廓"})
        result["proof"] = call("POST", "proofs/"+proof["id"]+"/decide", {"state":"accepted", "note":"通过"})["state"]
        if flow == "quarantine-plate":
            call("POST", route+"/quarantine", {"reason":"撤回"})
            require(call("GET", "proofs/"+proof["id"])["state"] == "invalidated", "commission", "Proof remains accepted", 500)
            call("POST", route+"/purge")
            result["rejected"] = rejected("GET", route+"/bundle", {}, "plate_state")
    elif flow == "export-bundle":
        bundle = call("GET", route+"/bundle")[0]
        require(bundle == call("GET", route+"/bundle")[0], "commission", "Bundle changed", 500)
        result["bundle_bytes"] = len(bundle)
    if "rejected" not in result:
        result["rejected"] = rejected("POST", eroute+"/cancel", {}, "state")
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", choices=FLOWS, default="render-plates")
    parser.add_argument("--output")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="facetproof-") as root:
        print(json.dumps(commission(root, args.flow, args.output), ensure_ascii=False, indent=2))
if __name__ == "__main__": main()

import copy,hashlib,json
from pathlib import Path
out=Path('docs/phase-e/dev-baseline-adapter-20261023')
def sha(data):return hashlib.sha256(data).hexdigest()
source=json.loads(Path('chaos/source_qual_v1.json').read_text())
manifest=json.loads(Path('chaos/manifest_qual_v1.json').read_text())
original=copy.deepcopy(source)
ci=Path('.github/workflows/ci.yml').read_text()
ci=ci.replace('    env:\n      SAFEACT_TOXIPROXY_UPSTREAM_HOST: "127.0.0.1"\n','',1)
ci=ci.replace('          # Linux host networking lets the proxy reach the harness\'s loopback\n          # mock servers. The proxy API itself listens only on loopback.\n          docker run -d --name safeact-toxiproxy --network host \\\n            ghcr.io/shopify/toxiproxy:2.12.0@sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e \\\n            -host=127.0.0.1\n','',1)
ci=ci.replace('              && docker exec safeact-redis redis-cli ping 2>/dev/null | grep -q PONG \\\n              && curl --fail --silent http://127.0.0.1:8474/version >/dev/null; then','              && docker exec safeact-redis redis-cli ping 2>/dev/null | grep -q PONG; then',1)
qualification=Path('.github/workflows/qualification.yml').read_text()
qualification=qualification.replace('SAFEACT_TOXIPROXY_UPSTREAM_HOST: "127.0.0.1"','SAFEACT_TOXIPROXY_UPSTREAM_HOST: "172.17.0.1"',1)
qualification=qualification.replace('          docker run -d --name safeact-toxiproxy --network host \\\n            ghcr.io/shopify/toxiproxy:2.12.0@sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e \\\n            -host=127.0.0.1','          docker run -d --name safeact-toxiproxy \\\n            -p 127.0.0.1:8474:8474 -p 127.0.0.1:21000-21062:21000-21062 \\\n            ghcr.io/shopify/toxiproxy:2.12.0@sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e',1)
old={'.github/workflows/ci.yml':sha(ci.encode()),'.github/workflows/qualification.yml':sha(qualification.encode())}
for entry in original['files']:
 if entry['path'] in old:entry['sha256']=old[entry['path']]
source_bytes=(json.dumps(original,indent=2)+'\n').encode()
old_manifest=copy.deepcopy(manifest);old_manifest['inputs']['source']['sha256']=sha(source_bytes)
manifest_bytes=(json.dumps(old_manifest,indent=2)+'\n').encode()
expected=json.loads(Path('/tmp/safeact-prestop2-integration-corrected-inventory.json').read_text())['manifest_sha256']
assert sha(manifest_bytes)==expected,(sha(manifest_bytes),expected)
(out/'integration-start-source-sha256.json').write_bytes(source_bytes)
(out/'integration-start-manifest.json').write_bytes(manifest_bytes)
changed=[entry['path'] for entry,before in zip(source['files'],original['files']) if entry!=before]
assert changed==sorted(old)
(out/'integration-source-proof.json').write_text(json.dumps(dict(scope='Only the two CI workflow files changed after full integration started; Python SUT bytes unchanged',integration_manifest_sha256=expected,current_manifest_sha256=sha(Path('chaos/manifest_qual_v1.json').read_bytes()),changed_files=changed,python_sut_changes=[]),indent=2)+'\n')
print('Verified original integration manifest exactly; only two CI YAML files changed, no Python SUT bytes changed')

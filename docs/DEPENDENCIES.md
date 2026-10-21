# Shop dependency environments

Shops can use operator-provisioned Python libraries in source builds and hosted
Python programs. Dependencies are selected when building the worker image;
submitting a project never installs a package. The API and broker retain their
own locked dependencies.

## Build and select an environment

The default `./scripts/build-worker` produces the core environment. To add
libraries, provide a reviewed requirements lock containing exact versions and
SHA-256 hashes for the dependency closure. Only binary wheels from PyPI are
accepted. URLs, editable installs, local paths, additional indexes, environment
markers and pip directives are rejected. Generate a lock for the worker's
Python version and Linux architecture, rather than the author's host.

For the supplied CadQuery example on Linux x86-64:

```sh
uv export --locked --no-dev --no-emit-project --no-hashes \
  --format requirements-txt --output-file .local/core-constraints.txt
uv pip compile deploy/worker/cadquery.in --python-version 3.12 \
  --python-platform x86_64-manylinux_2_31 --only-binary :all: \
  --generate-hashes --no-header --no-annotate \
  --constraint .local/core-constraints.txt --output-file .local/candidate-cadquery.txt
```

Review a newly resolved lock before using it. The committed
`deploy/worker/cadquery.txt` is the tested lock; resolution at a later date can
select different transitive versions. Installing that lock uses:

```sh
./scripts/build-worker --graphics --requirements deploy/worker/cadquery.txt \
  --output .local/cadquery-worker-image
```

The command prints the immutable image ID and dependency digest. An explicit
output path leaves `.local/worker-image` unchanged. Omitting `--output` selects
the resulting image for subsequent local/runtime startup. A failed build leaves
the previous image file intact. Existing running deployments keep their selected
image; changing it requires the deployment's normal maintenance and restart
procedure. Rebuild runtime images when the Chipload SDK changes.

One environment is selected per broker/deployment. A shop agent cannot select an
arbitrary image, add a library, or increase resource limits. Operators should
retain the reviewed lock and immutable image needed to reproduce historical
builds. Other architectures require their own resolved and tested lock.

## Pin the source project

`SourceProject.dependencies_sha256` optionally requires the worker's exact
dependency digest. A mismatch produces a `dependency-mismatch` diagnostic before
source execution. Changing the pin creates a new source revision. Projects
without a pin retain their existing serialization and revision IDs; their build
artifacts still record the actual environment.

Export the CadQuery project without installing CadQuery on the host:

```sh
worker_image=$(cat .local/cadquery-worker-image)
dependency_digest=$(docker image inspect "$worker_image" \
  --format '{{index .Config.Labels "org.chipload.dependencies-sha256"}}')
uv run python -m examples.cadquery_shop "$dependency_digest" > .local/cadquery-project.json
```

Use the normal [source/build/publication workflow](AUTHORING.md) with this source
bundle and image. The example computes a fixture envelope from a CadQuery solid
and composes geometric fit with prototype and drawing-approval conditions. Its
six acceptance cases cover support, oversize, commercial rejection, conditional
approval, missing geometry and refusal of simulated evidence. Geometry calculations do not establish cutting
loads, material suitability or physical qualification.

The dependency digest covers the core lock and canonical extra requirements,
plus the image recipe when the graphics profile is selected.
Versions, hashes and package names affect the digest; comments and ordering in
the Python requirements lock do not. The artifact also records the SDK digest, Python version and immutable
image ID. Those identify the build environment, not the trustworthiness of a
package or the reproducibility of arbitrary nondeterministic shop code.

## Execution boundaries

The worker image contains two Python environments. Untrusted shop source and
hosted Python use `/opt/chipload-shop/bin/python`, with access to the core SDK and
the additional libraries. The trusted SDK service explicitly uses
`/usr/local/bin/python`; it cannot import the shop environment's packages or
execute their startup hooks. API, broker and executor images contain no shop
dependencies. The separate container and its restrictions enforce isolation;
a Python virtual environment alone is not a security boundary.

Package installation uses pip's hash checking and wheel-only mode, and constrains
core package versions to the SDK lock. Downloading fetches only the packages
explicitly listed in the reviewed lock. Installation resolves the complete
dependency set offline with the build container's network disabled; missing
dependencies and conflicting core versions fail the build. The image build does not invoke the shop
interpreter after installation, so dependency startup hooks first run inside a
restricted source/program container. No dependency code is imported on the host
to generate or inspect source bundles. See [pip's secure installation
guidance](https://pip.pypa.io/en/stable/topics/secure-installs/) and [Python's
virtual environment behavior](https://docs.python.org/3.12/library/venv.html).

`--graphics` adds GL/X11 loader libraries from signed Debian and Debian security
snapshots dated 2026-10-21, pinning the transitive native package versions.
The Expat XML parser is pinned to `2.9.0-1~deb13u1`, containing the fixes in
[DSA-6566-1](https://security-tracker.debian.org/tracker/DSA-6566-1).
This profile supports geometry libraries that import those loaders even when no
display is used.
The default image omits them. Updating the snapshot requires rebuilding and
retesting the environment; it is not an automatic security update mechanism.
The graphics recipe participates in the dependency digest. Update the project's
environment pin and rebuild before publishing it against the replacement image.
Existing source revisions retain their original pin.

Workers retain their existing network, filesystem, CPU, memory, process, output
and execution limits. Libraries requiring additional OS packages or more
resources require an explicitly designed operator environment. Python package
hashes do not qualify a dependency as safe; review and maintain the selected
packages. Extra libraries cannot receive the repository mount, service settings,
equipment credentials or the Docker socket.

## Verification

```sh
./scripts/build-worker --output .local/core-worker-image
./scripts/build-worker --graphics --requirements deploy/worker/cadquery.txt \
  --output .local/cadquery-worker-image
CHIPLOAD_DEPENDENCY_TESTS=1 uv run --extra server python -m unittest discover \
  -s tests -p 'test_worker_dependencies_live.py' -v
```

The live checks build the same CadQuery project twice, compare artifact identity,
run its technical/business cases, compose a CadQuery calculation with an actual
SDK call, and verify the core interpreter cannot import CadQuery. A separate
image contains an intentional startup hook that refuses to
run when database storage is mounted; source and Python execution run the hook
while actual scoped SDK calls continue through the clean interpreter.
An incorrect wheel hash fails the build without replacing the selected image.

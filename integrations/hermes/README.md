# OpenADA adapter for Hermes

This directory builds the separate `openada-hermes-plugin` wheel. Its
`hermes_agent.plugins` entry point registers the canonical OpenADA skills as
advertised, read-only skills and does not add model tools.

Build both artifacts from the same reviewed OpenADA repository revision:

```bash
python -m build --wheel --outdir dist/runtime
python -m build --wheel --outdir dist/hermes integrations/hermes
python -m pip install \
  dist/runtime/openada-*.whl \
  dist/hermes/openada_hermes_plugin-*.whl
```

The adapter distribution version and its exact `openada` dependency must stay
equal to the root runtime version. The build hook copies the root `skills/`
tree into only the adapter wheel.

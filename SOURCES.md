# Reference and detection sources

- The bundled cross-machine curves are normalized from the public test fixtures in the official [Artisan repository](https://github.com/artisan-roaster-scope/artisan/tree/master/src/test/sanity/data/artisan), which is GPL-3.0 licensed. Their event markers are operator annotations, not sensor-verified truth.
- Artisan documents that probe size/noise and smoothing change curve rendering, so temperature values from different machines and probes are not treated as interchangeable: [Artisan curve documentation](https://artisan-scope.org/docs/curves/).
- Public crack-audio work currently offers first-crack/no-first-crack labels, but not a complete FCs/FCe/SCs/SCe thermal-curve truth set: [coffee-first-crack-detection](https://huggingface.co/syamaner/coffee-first-crack-detection).

The companion therefore uses cross-machine references for time/shape priors only. Same-machine history may also contribute temperature evidence. Acoustic pop clusters and manual observations carry more weight for crack events.

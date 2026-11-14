# Locove 0.1.0 source preview

Locove can register and run local services, open their URLs, show logs, and diagnose failures such as port conflicts. The menu bar and workbench support English and Simplified Chinese. Some diagnostic output can still appear in Chinese.

The preview builds the menu bar app on your Mac. See the [installation guide](../README.md#install). If you downloaded the source archive and its checksum, run `shasum -a 256 -c locove-0.1.0.tar.gz.sha256` from their download directory before extracting the archive.

To update, replace the source in the same directory and rerun `bash scripts/install.sh`. To remove Locove, run `bash scripts/uninstall.sh`. Add `--purge` to remove Locove's settings, registrations, and logs as well. Your project files are kept.

Existing registrations and language choices carry over. If you used a development checkout with bundled private manifests, move the TOML files you still use into `~/Library/Application Support/LocalServices/builtin-services/` before updating.

For a bug report, include your Locove version, macOS version, Mac architecture, and a redacted error or log.

## Publish a source release

Run **Source release** from the GitHub Actions tab to preview the archive. The workflow uploads the archive and checksum to the run's **Artifacts**.

Push a version tag such as `v0.1.0` from a commit that contains the workflow. The tag run attaches `locove-0.1.0.tar.gz` and its checksum to the GitHub Release.

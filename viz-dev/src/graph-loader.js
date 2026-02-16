/**
 * Graph data loading utilities.
 * Fetches graph JSON and manifest from /data/ directory.
 */

const MANIFEST_URL = '/data/manifest.json';

export async function loadManifest() {
  const resp = await fetch(MANIFEST_URL);
  if (!resp.ok) throw new Error(`Failed to load manifest: ${resp.status}`);
  return resp.json();
}

export async function loadGraphData(id) {
  const resp = await fetch(`/data/${id}.json`);
  if (!resp.ok) throw new Error(`Failed to load graph "${id}": ${resp.status}`);
  return resp.json();
}

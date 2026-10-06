Static podcast feed. Only the feed lives here (feed.xml, episodes.json, cover.jpg); episode audio is
served from object storage (Cloudflare R2), configured in `podcast.json` under `storage`. Never commit audio.

Add an episode (uploads the MP3 to storage, then rebuilds and pushes the feed):

    ./publish_episode.py add path/to/episode.mp3 --title "Title" --description "Short description"

Credentials come from the environment at runtime only: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID`.
Requires `boto3` (`python3 -m pip install --user boto3`).

Other commands: `remove FILE|SLUG|GUID [--keep-file]` (also deletes the object from storage),
`migrate` (uploads any MP3s left in a local episodes/ folder), `build` (rebuild feed only).

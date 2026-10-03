"""Control service for the tournament streaming stack: logins, slots, status, delay, archive.

Entry points (one image, several services in docker-compose.yml):
  python -m mediactl.server     bot API (bearer token) + MediaMTX auth hook
  python -m mediactl.delay      one delayed-feed player per slot
  python -m mediactl.archiver   copy recordings to node 1, prune the SSD ring buffer
  python -m mediactl.stitch     (node 1) cut one MP4 per team per match from the archive
"""

# The official MediaMTX image is built from scratch and has no CA
# certificates, which it needs to reach Cloudflare over HTTPS.
FROM alpine:3.22 AS certs
RUN apk add --no-cache ca-certificates

# Official MediaMTX image (static binary; its entrypoint reads /mediamtx.yml)
FROM bluenviron/mediamtx:1.21.2
COPY --from=certs /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY mediamtx.yml /mediamtx.yml

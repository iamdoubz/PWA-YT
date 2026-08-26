#!/bin/sh
# Open Graph link-preview URLs, resolved at container start rather than at
# image build.
#
# The public origin is deployment configuration, not build configuration: one
# published image is pulled by many deployments, each on its own hostname, so
# it cannot be baked in. nginx's own entrypoint runs everything in
# /docker-entrypoint.d/ before starting the server, which is the hook this uses.
#
# Rewrites from a pristine template rather than in place, so changing
# PUBLIC_ORIGIN and restarting takes effect instead of stacking onto the
# previous value.
set -eu

ROOT=/usr/share/nginx/html
TMPL=/usr/share/pwa-yt/index.html.tmpl

# No template means an image built before this existed. Leave the shell alone.
[ -f "$TMPL" ] || exit 0

ORIGIN=$(printf '%s' "${PUBLIC_ORIGIN:-}" | sed 's#/*$##')

if [ -z "$ORIGIN" ]; then
  # Unset is a supported configuration, not an error: the tags stay relative,
  # which Slack/iMessage/Discord resolve against the page URL anyway. Only
  # Facebook and X need them absolute.
  cp "$TMPL" "$ROOT/index.html"
  echo "[og] PUBLIC_ORIGIN unset — link-preview URLs left relative"
  exit 0
fi

# & and the # delimiter are the only characters special in the replacement.
ESCAPED=$(printf '%s' "$ORIGIN" | sed 's#[&\#\]#\&#g')

sed -e "s#\(property=\"og:url\" content=\"\)/\"#\1${ESCAPED}/\"#" \
    -e "s#content=\"/imgs/opengraph.png\"#content=\"${ESCAPED}/imgs/opengraph.png\"#g" \
    "$TMPL" > "$ROOT/index.html"

echo "[og] link-preview URLs absolute at ${ORIGIN}"

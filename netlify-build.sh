#!/bin/sh
set -eu
mkdir -p netlify-site
cp daytrade-dashboard.html netlify-site/index.html
cp daytrade-sw.js netlify-site/sw.js
test -s netlify-site/index.html
test -s netlify-site/sw.js
test -s netlify-site/manifest.webmanifest
test -s netlify-site/push-icon.svg
echo "Netlify static frontend prepared; backend API remains protected on Render."

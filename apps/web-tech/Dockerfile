# The SPA is built before packaging (azd's prepackage hook and deploy-apps.yml run
# `npm run build` with the public VITE_* settings), so this image only serves that
# prebuilt dist. Dockerfile.local builds from source inside Docker instead.
FROM nginx:1.27-alpine
COPY dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80

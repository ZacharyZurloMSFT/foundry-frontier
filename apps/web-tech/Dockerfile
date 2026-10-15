# Preserve the existing prepackaged dist by default. An explicit business opt-in
# rebuilds the SPA with these public, build-time Vite settings.
FROM node:20-alpine AS build
WORKDIR /src
ARG VITE_API_BASE_URL=http://localhost:8000
ARG VITE_INSURANCE_BUSINESS_ENABLED=false
ARG VITE_INSURANCE_AUTH_TENANT_ID=
ARG VITE_INSURANCE_AUTH_CLIENT_ID=
ARG VITE_INSURANCE_AUTH_SCOPE=
ARG VITE_INSURANCE_RUN_ID=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_INSURANCE_BUSINESS_ENABLED=${VITE_INSURANCE_BUSINESS_ENABLED} \
    VITE_INSURANCE_AUTH_TENANT_ID=${VITE_INSURANCE_AUTH_TENANT_ID} \
    VITE_INSURANCE_AUTH_CLIENT_ID=${VITE_INSURANCE_AUTH_CLIENT_ID} \
    VITE_INSURANCE_AUTH_SCOPE=${VITE_INSURANCE_AUTH_SCOPE} \
    VITE_INSURANCE_RUN_ID=${VITE_INSURANCE_RUN_ID}
COPY . .
RUN if [ "$VITE_INSURANCE_BUSINESS_ENABLED" = "true" ]; then \
      npm ci --no-audit --no-fund && npm run build; \
    fi

FROM nginx:1.27-alpine
COPY --from=build /src/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80

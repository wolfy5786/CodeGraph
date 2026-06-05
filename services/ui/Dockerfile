# Build context must be the repo root so both paths below are reachable.
# In docker-compose: build: { context: ., dockerfile: services/ui/Dockerfile }

FROM nginx:1.27-alpine

# Remove the default site config
RUN rm /etc/nginx/conf.d/default.conf

COPY services/ui/nginx.conf      /etc/nginx/conf.d/default.conf
COPY services/api/app/static/index.html  /usr/share/nginx/html/index.html

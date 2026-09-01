FROM node:24-alpine AS build

WORKDIR /app
ARG VITE_REPOSCOPE_MODE=static
ARG VITE_API_BASE_URL=
ENV VITE_REPOSCOPE_MODE=$VITE_REPOSCOPE_MODE
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80

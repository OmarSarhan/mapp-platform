FROM mcr.microsoft.com/playwright:v1.61.1-noble@sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48
WORKDIR /runner
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY server.mjs ./
RUN apt-get purge --yes \
      gstreamer1.0-plugins-bad \
      libgstreamer-plugins-bad1.0-0 \
    && rm -rf /usr/lib/node_modules/npm \
    && rm -f /usr/bin/npm /usr/bin/npx
USER pwuser
CMD ["node", "server.mjs"]

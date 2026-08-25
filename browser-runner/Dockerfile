FROM mcr.microsoft.com/playwright:v1.62.1-noble@sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e
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

FROM mcr.microsoft.com/playwright:v1.61.1-noble
WORKDIR /runner
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY server.mjs ./
USER pwuser
CMD ["node", "server.mjs"]

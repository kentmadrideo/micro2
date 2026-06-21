FROM node:18-alpine

WORKDIR /usr/src/app

# Install production dependencies
COPY package*.json ./
RUN npm ci --only=production

# Copy app
COPY . .

ENV NODE_ENV=production

EXPOSE 3000

CMD ["node", "server.js"]

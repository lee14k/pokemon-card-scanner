// Metro, extended to compile ../shared — the API client, wire types, copy and
// theme tokens this app shares with the web frontend.
const path = require("path");
const { getDefaultConfig } = require("expo/metro-config");

const projectRoot = __dirname;
const sharedRoot = path.resolve(projectRoot, "../shared");

const config = getDefaultConfig(projectRoot);

config.watchFolders = [...(config.watchFolders ?? []), sharedRoot];

// `@shared/foo` → ../shared/src/foo (mirrors the tsconfig `paths` entry).
const resolveRequest = config.resolver.resolveRequest;
config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (moduleName === "@shared" || moduleName.startsWith("@shared/")) {
    const rest = moduleName.slice("@shared".length).replace(/^\//, "") || "index";
    return context.resolveRequest(context, path.join(sharedRoot, "src", rest), platform);
  }
  return (resolveRequest ?? context.resolveRequest)(context, moduleName, platform);
};

module.exports = config;

/**
 * Invoke npm without a shell or a Windows .cmd shim. npm/npx expose their JS
 * launcher path to child tests; standalone POSIX runners can still use PATH.
 */
import { existsSync } from "node:fs";
import path from "node:path";

/** Resolve the npm launcher, including when this suite was started through npx. */
export function npmCommand(args: string[]): [string, string[]] {
  const launcher = process.env.npm_execpath;
  if (launcher) {
    const cli = path.join(path.dirname(launcher), "npm-cli.js");
    if (existsSync(cli)) return [process.execPath, [cli, ...args]];
  }
  return ["npm", args];
}

/**
 * npm 12 re-exports .npmrc's allow-scripts as an environment variable, then
 * rejects that form for nested local installs. Let the child read .npmrc itself;
 * packaging tests additionally disable install scripts for their temporary apps.
 */
export function npmInstallEnvironment(): NodeJS.ProcessEnv {
  return Object.fromEntries(Object.entries(process.env)
    .filter(([key]) => key.toLowerCase() !== "npm_config_allow_scripts"));
}

import { DurableObject } from 'cloudflare:workers';

const publishVariables = [
  'THREADS_ACCOUNT_ID',
  'THREADS_ACCESS_TOKEN',
  'OPENROUTER_API_KEY',
  'OPENROUTER_MODEL',
  'FEEDROOM_R2_ACCOUNT_ID',
  'FEEDROOM_R2_BUCKET',
  'FEEDROOM_R2_ACCESS_KEY_ID',
  'FEEDROOM_R2_SECRET_ACCESS_KEY',
];

export class DailyContainer extends DurableObject {
  async run() {
    const container = this.ctx.container;
    if (!container.running) {
      const mode = this.env.FEEDROOM_MODE;
      if (mode !== 'preview' && mode !== 'publish') {
        throw new Error('FEEDROOM_MODE must be preview or publish.');
      }
      const env = {};
      if (mode === 'publish') {
        for (const name of publishVariables) {
          const value = this.env[name]?.trim();
          if (!value) throw new Error(`Missing Cloudflare binding ${name}.`);
          env[name] = value;
        }
      }
      container.start({
        image: container.images.base,
        instance: 'lite',
        enableInternet: true,
        entrypoint: ['feedroom', mode],
        env,
      });
    }
    await container.monitor();
  }
}

export default {
  async scheduled(_controller, env) {
    await env.DAILY_CONTAINER.getByName('daily').run();
  },
};

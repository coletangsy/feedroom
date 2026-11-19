import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test from 'node:test';
import configure from '../cloudflare.config.ts';

const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'cloudflare:workers') {
      return {
        url: 'data:text/javascript,export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }',
        shortCircuit: true,
      };
    }
    return nextResolve(specifier, context);
  },
});
const { default: worker, DailyContainer } = await import('../deployment/cloudflare.mjs');
hooks.deregister();

test('configuration schedules 08:00 HKT and only explicit non-preview deploys can publish', () => {
  const daily = configure({ isPreview: false }).worker;
  assert.deepEqual(daily.triggers, [{ type: 'scheduled', schedule: '0 0 * * *' }]);
  assert.equal(daily.env.FEEDROOM_MODE.value, 'preview');
  assert.equal(daily.env.THREADS_ACCESS_TOKEN, false);
  const publication = configure({ mode: 'publish', isPreview: false }).worker;
  assert.equal(publication.env.FEEDROOM_MODE.value, 'publish');
  assert.equal(publication.env.THREADS_ACCESS_TOKEN.type, 'secret');
  assert.equal(configure({ mode: 'publish', isPreview: true }).worker.env.FEEDROOM_MODE.value, 'preview');
});

test('scheduled preview starts the shared image without publishing credentials', async () => {
  const container = {
    images: { base: 'fixture-image' },
    start(options) {
      assert.deepEqual(options, {
        image: 'fixture-image', instance: 'lite', enableInternet: true,
        entrypoint: ['feedroom', 'preview'], env: {},
      });
      this.running = true;
    },
    monitor() { assert(this.running); },
  };
  const job = new DailyContainer({ container }, { FEEDROOM_MODE: 'preview', THREADS_ACCESS_TOKEN: 'synthetic' });
  await worker.scheduled({}, { DAILY_CONTAINER: { getByName(name) { assert.equal(name, 'daily'); return job; } } });
});

test('publish requires every binding and will not fall back to the container disk', async () => {
  const container = {
    images: { base: 'fixture-image' },
    start() { assert.fail('started without publishing bindings'); },
  };
  const job = new DailyContainer({ container }, { FEEDROOM_MODE: 'publish' });
  await assert.rejects(job.run(), /Missing Cloudflare binding THREADS_ACCOUNT_ID/);
});

test('explicit publish passes the R2 and Threads bindings and reports process failures', async () => {
  const secrets = Object.fromEntries([
    'THREADS_ACCOUNT_ID', 'THREADS_ACCESS_TOKEN', 'OPENROUTER_API_KEY', 'OPENROUTER_MODEL', 'FEEDROOM_R2_ACCOUNT_ID',
    'FEEDROOM_R2_BUCKET', 'FEEDROOM_R2_ACCESS_KEY_ID', 'FEEDROOM_R2_SECRET_ACCESS_KEY',
  ].map((name) => [name, `fixture-${name}`]));
  const container = {
    images: { base: 'fixture-image' },
    start(options) {
      assert.deepEqual(options.entrypoint, ['feedroom', 'publish']);
      assert.deepEqual(options.env, secrets);
    },
    monitor() { throw new Error('fixture process failed'); },
  };
  const job = new DailyContainer({ container }, { FEEDROOM_MODE: 'publish', ...secrets });
  await assert.rejects(job.run(), /fixture process failed/);
});

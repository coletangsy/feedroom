import { bindings, defineConfig, defineContainer, exports, triggers } from 'cf/config';

const name = 'feedroom-daily';
const container = defineContainer({
  name,
  schedulingPolicy: 'durable-object',
  images: { base: { dockerfile: './Dockerfile' } },
});

export default defineConfig(({ mode, isPreview }) => {
  const publish = mode === 'publish' && !isPreview;
  return {
    worker: {
      name,
      entrypoint: './deployment/cloudflare.mjs',
      compatibilityDate: '2026-11-19',
      workersDev: false,
      previewUrls: false,
      observability: { enabled: true },
      exports: { DailyContainer: exports.durableObject({ storage: 'sqlite', container }) },
      env: {
        DAILY_CONTAINER: bindings.durableObject({ worker: name, exportName: 'DailyContainer' }),
        FEEDROOM_MODE: bindings.text(publish ? 'publish' : 'preview'),
        THREADS_ACCOUNT_ID: publish && bindings.secret(),
        THREADS_ACCESS_TOKEN: publish && bindings.secret(),
        OPENROUTER_API_KEY: publish && bindings.secret(),
        OPENROUTER_MODEL: publish && bindings.secret(),
        FEEDROOM_R2_ACCOUNT_ID: publish && bindings.secret(),
        FEEDROOM_R2_BUCKET: publish && bindings.secret(),
        FEEDROOM_R2_ACCESS_KEY_ID: publish && bindings.secret(),
        FEEDROOM_R2_SECRET_ACCESS_KEY: publish && bindings.secret(),
      },
      triggers: [triggers.scheduled({ schedule: '0 0 * * *' })],
    },
    containers: [container],
  };
});

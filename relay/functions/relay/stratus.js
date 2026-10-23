'use strict';
/**
 * Object storage for forwarded attachments.
 *
 * An image sent from Cliq cannot be fetched by the laptop directly: `file.url`
 * carries `?type=transient` and returns `attachment_access_time_expired` within
 * minutes, and the laptop holds no Cliq credential anyway -- only a webhook
 * token, which posts and cannot read. The bot's Deluge handler is the one
 * context authorised to read the bytes, so it uploads them here and the laptop
 * collects them with a pre-signed GET.
 *
 * Nothing about the object is stored anywhere. Its key is DERIVED from the
 * owning account and the message id, which is what keeps one paired user from
 * reaching another's image: `/file-url` derives the key from the requesting
 * DEVICE's account, never from an identity the caller supplied, so a device can
 * only ever address its own namespace no matter what message id it asks for.
 */

const catalyst = require('zcatalyst-sdk-node');

const PUT_TTL = '10m';     // the laptop fetches within seconds of the bell
const MAX_BYTES = 10 * 1024 * 1024;

function createFiles({ req, bucketName }) {
  const configured = !!bucketName;

  // Built per request like every other component: the SDK wants an app
  // initialised from the request headers, not from process env.
  const bucket = () => catalyst.initialize(req).stratus().bucket(bucketName);

  return {
    configured,
    bucketName,

    /** Does the bucket actually exist? Answered without the console. */
    async check() {
      const stratus = catalyst.initialize(req).stratus();
      const buckets = await stratus.listBuckets();
      const names = buckets.map((b) => (b.getName ? b.getName() : String(b)));
      return { names, present: names.includes(bucketName) };
    },

    async put(key, body) {
      if (body.length > MAX_BYTES) {
        const e = new Error('file too large'); e.status = 413; throw e;
      }
      await bucket().putObject(key, body);
      return true;
    },

    async signedGet(key) {
      // headObject first: a pre-signed URL is minted happily for a key that
      // does not exist, and the laptop would then download an error document
      // and save it as a .png.
      const there = await bucket().headObject(key, false);
      if (!there) return null;
      const res = await bucket().generatePreSignedUrl(key, 'GET', { expiryIn: PUT_TTL });
      return (res && (res.signature || res.url || res.signed_url)) || null;
    },

    async remove(key) {
      try {
        await bucket().deleteObject(key);
        return true;
      } catch (err) {
        // A lingering object is harmless; losing the reply is not.
        console.warn('stratus delete failed', err && err.message);
        return false;
      }
    },
  };
}

module.exports = { createFiles, MAX_BYTES, PUT_TTL };

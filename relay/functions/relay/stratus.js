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

// SECONDS, as a string -- not a duration like '10m', which the API takes
// without complaint as expiry_in_seconds and then refuses to sign.
//
// Short on purpose. The laptop fetches within a second of the doorbell, so
// this is only slack for a cold start -- and since a deleted object keeps
// being served from the edge until its URL expires, this expiry IS the
// cleanup guarantee.
const URL_TTL_SECONDS = '120';
const MAX_BYTES = 10 * 1024 * 1024;

function createFiles({ req, bucketName }) {
  const configured = !!bucketName;

  // Built per request like every other component: the SDK wants an app
  // initialised from the request headers, not from process env.
  const bucket = () => catalyst.initialize(req).stratus().bucket(bucketName);

  return {
    configured,
    bucketName,

    /**
     * Does the bucket exist, and what is sitting in it?
     *
     * The object count matters because `deleteObject` only SCHEDULES a
     * deletion and an object was measured still present ten minutes later --
     * so this is the only way to tell whether anything is actually being
     * swept, short of opening the console.
     */
    async check() {
      const stratus = catalyst.initialize(req).stratus();
      const buckets = await stratus.listBuckets();
      const names = buckets.map((b) => (b.getName ? b.getName() : String(b)));
      const present = names.includes(bucketName);
      let holding = null;
      if (present) {
        try {
          const d = await bucket().getDetails();
          holding = { objects: Number(d.objects_count || 0),
                      bytes: Number(d.size_in_bytes || 0) };
        } catch (err) {
          holding = { error: (err && err.message) || 'details unavailable' };
        }
      }
      return { names, present, holding };
    },

    /**
     * Is one specific key there?
     *
     * getDetails() reports bucket STATISTICS, which are computed periodically
     * and lag behind an upload -- so a freshly uploaded object shows an
     * objects_count of 0 and looks missing when it is not. headObject is
     * authoritative and immediate, which is what a "did my upload land, and
     * under what key" question actually needs.
     */
    async head(key) {
      const o = await bucket().headObject(key, { throwErr: false });
      if (!o) return { key, found: false };
      return { key, found: true,
               size: Number((o && (o.size || o.content_length)) || 0) || null };
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
      //
      // The second argument is an OPTIONS OBJECT, not a boolean. Passing
      // `false` positionally leaves throwErr undefined, which defaults to
      // throwing -- so a missing object surfaced as a raw Catalyst error
      // instead of the 404 the laptop knows how to report.
      const there = await bucket().headObject(key, { throwErr: false });
      if (!there) return null;
      const res = await bucket().generatePreSignedUrl(key, 'GET',
                                                      { expiryIn: URL_TTL_SECONDS });
      return (res && (res.signature || res.url || res.signed_url)) || null;
    },

    /**
     * Ask for the object to go away.
     *
     * Stratus answers "Object Deletion scheduled." -- deletion is ASYNC, so the
     * object is still downloadable for a while after a successful call, and
     * headObject still reports it. That is harmless here: the key is derived
     * from the account and the message id, so a lingering object can never
     * collide with a later one, and nothing reads it twice.
     */
    async remove(key) {
      try {
        const res = await bucket().deleteObject(key);
        // The store empties promptly -- the bucket reported 0 objects while a
        // signed URL for a deleted key was still returning HTTP 200 with the
        // full bytes, so what lingers is the edge cache, not the object.
        //
        // purgeCache() would drop it, but the SDK sends that call as
        // CREDENTIAL_USER.admin and a function initialised from request
        // headers runs in user scope, so it can never succeed here. Rather
        // than make a call that always fails, the exposure is bounded by the
        // signed URL's own short expiry instead -- and such a URL is only ever
        // issued to the device that owns the picture.
        return { scheduled: true, said: (res && res.message) || '' };
      } catch (err) {
        // A lingering object is harmless; losing the reply is not.
        console.warn('stratus delete failed', err && err.message);
        return { scheduled: false, said: (err && err.message) || 'failed' };
      }
    },
  };
}

module.exports = { createFiles, MAX_BYTES, URL_TTL_SECONDS };

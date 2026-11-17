async function prepareReferencePhoto(file, index) {
  const bytes = new Uint8Array(await file.slice(0, 128).arrayBuffer());
  const header = String.fromCharCode(...bytes);
  // A photo picker may supply a different filename or MIME type.
  const heif = header.slice(4, 8) === 'ftyp'
    && (/(heic|heix|hevc|hevx|heim|heis|hevm|hevs|mif1|msf1)/.test(header.slice(8))
        || /\.(heic|heif)$/i.test(file.name) || /^image\/hei[cf]$/i.test(file.type));
  if (!heif) return file;

  const url = URL.createObjectURL(file.slice(0, file.size, 'image/heic'));
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    const scale = Math.min(1, 1600 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    canvas.getContext('2d', { alpha: false }).drawImage(image, 0, 0, canvas.width, canvas.height);
    const jpeg = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.92));
    if (!jpeg) throw new Error('Could not encode photo');
    return jpeg;
  } catch {
    throw new Error(`This browser could not open HEIC photo ${index}. Export it as JPEG and select it again.`);
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function referencePhotoUpload(files, details) {
  const mb = 1024 * 1024;
  if (files.length === 0 || files.length > 12) {
    throw new Error('Choose between 1 and 12 reference photos.');
  }
  if (files.some(({ size }) => size < 1 || size > 10 * mb)) {
    throw new Error('Choose nonempty photos up to 10 MB each.');
  }
  const prepared = [];
  for (const [index, file] of files.entries()) {
    prepared.push(await prepareReferencePhoto(file, index + 1));
  }
  if (prepared.some(({ size }) => size > 10 * mb) || prepared.reduce((total, { size }) => total + size, 0) > 40 * mb) {
    throw new Error('Choose photos up to 10 MB each and 40 MB total.');
  }
  const photos = prepared.map(({ size }) => ({ size }));
  return new Blob([JSON.stringify({ ...details, photos }) + '\n', ...prepared]);
}

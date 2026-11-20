import { expect, type Page, type Route } from "@playwright/test";

const background = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAUAAAACgCAIAAADywSLLAAAG6klEQVR4nO2dTW7jRhBGOUSuZ3jH9czJMusGAsQwkCtkP7vcJgsBjGNZFH+6q+qrem8VJFbI7urHr9iSqG9///pnAgBNZu8TAIDzIDCAMAgMIAwCAwiDwADCIDCAMAgMIAwCAwiDwADCIDCAMAgMIAwCAwiDwAD/0d7fJikQGOB/9p5z2Mv8b3ydEGC6M3B5eT3xwv2v6gUJDDDd5+fORHVvuUlgqE7blPBRqD56lXEIk8AAW3wpqnvwrpDAUJq2T8U1V/f8vWUIk8BQl7Y7SA9tUFvmMwJDUdpBzeK0zR9BYKhIG2yjme0IDOVoJnbZHAWBoRYtZCd8GgSGQjRbew0Oh8BQhZYre28gMIDwVQOBoQTNL36HHhqBIT8tY/N8A4EhOS2AvePOAYFDFBjSF7eNOZPqAq+fcY1TaehFK1DTnAJrPRUFRtDiVXPEKSUUeP8XR758DkPAwsNRWpkiZvs+8H3ljj5RYftVEJ8W296+SythAvdqp4OvAxCl77qaK0yN7iPL4CitWMnmIpU7XVeiWIgmYm/H80xyD7x/RpaX19PTx41xZJqIvX2X01ytclfKLLdE6tBGlmZ5eQ177c4gsCV01AXtnQKftrzALqlIFMehWS2AmCGsfQ/sLlLMotahDV4A9/XtfsSLS0g4gd3tDXIOZWnm9gYcharAcczhrjglywN7o/VcqgJHA43tJ3waxral3R2+MhZJgePEr8qJJUN023nEiPQ2sSQkGbcIvn//MUXi58/fa9rbYuxmiSWwhL1C5ylHEHvjDE1JYC0ruCseMaVTGHuXGLtZSgIronXRiUzAmVwC7GbJCBywfjshirvM4TSSJUacnhiphsC69q6g8ZWpm6Lau3ibLyBwAntTjiUHy2UDfRvp6ALnW/FE8dHpGlWJyT8/r486usBZyXdhqmzv4nchmGvW7/YVbff3/dB4e34KZu/R4c817f34z+4aOx49LHL2Lk6raK5s7/a/NIMovp+QcbO9DKu1y25WRIFdQilCFJPGtCSfeLokIgo8jqeKut8aFXc48lu+e7BfP3Px5vnLPyOKXVC316WRjiWwu72n/747xaO4O4t3QQcthkACx7F3fRVRbIboxpX74aIIHDZwImg8ZSeTvcZTFEXg4PVzdzixxintXayOG+KROtGa5w3cRfrrjz+nRI/USWmv5WN3/BNYyN4IayITue21mTFngbXsXf/PKReHMe69jAEG68RTYOkS4vAVcrzlG+Emzr+Fln7LPs5CEaKOvQYT6CawYvP86HDpV4wQS7xaDD0lH4HT2Ot+XDnYuOo7jQ4C57N3PToab1PW3mXYuVkLLL1xpb6MfClr79DdrDybWHFKSBTfU9zeQbT3N1OBszbPKqfkBfaOWxJ2Apey9wZRXOGmyXetGglc0N6Vyhrzlu9oLATmGixxoekO9hqsBO1NLC0rKkdxd5hJI4ErN8+ZTvsobFzZrIGxAmNvzSjG3qf0WgADBcbebbI6jL2WjBKYjauaUYy9++lSer1NrGQrPpPG2HuU63UfIjDN8wnUHabncqG/wNhbMIp5y/c0FyveWWDsvY6cw9jrSE+BaaJ6oRvF3akwD8uFMWpsYlWoouio2bjyrXU3gWmeR1A5issO3EFg7B1KQY2rjXc6O+QOAmOvDXXWdJ2RXh/4VYHZuLKkwsquMMaO/DZFhUJu/JgY182sLC+vh4p7KYFpnr3IelecclBDOS8w9rqTbLknG47NPJz8fWDsDUWCjhp7z9V0rrlckqHeUUufvC+xPolFIQvOnuhpB5mWwy00zXN8hFok7D1dytvUHUtg7JVAxQqV84w8dQcSGHvliBzF2HuuiJ/mbU6wFOARSJK+oHsTmPiVJtr1lyvLIdr726MZ2yUw9uYgiMbY25HnLTT2piGCORHOIRNPEhh7U+IVxdhrmsBBOi7IIRL25vkkFrWs9ulLKm4tMM1zBa54tf+12Gt9D4y91Tha8dXJpy/EXusExt6CnO6o8TOWwGxcVWanjZ/+bEN+9M6ziUUtq0UxFbcWmOYZnrq38z9hr/UmFvbC0a/CHP3gLoxKYOyFLzmhIvZaC8zGFey5K8bMcptYlDwNlDImM80zgC6jEpgLNoCqwNgLUPG50ADgLDDxC6AqMPYCqAqMvQCqAmMvgD1sYgFM1QUmfgFUBcZeAFWBsRdAVWDsBfCFTSyAkgITvwCqAmMvgKrA2AugKjD2AsSBTSwAYeZDiUr8Aqg+Fxp7AeK20Nt+Yi9A9HtgfqIKQHsTi6QFkP+B74+3xCgNoPc2Er+mASCcwAAQHz7IASAMAgMIg8AAwiAwgDAIDCAMAgMIg8AAwiAwgDAIDCAMAgMIg8AAky7/AnY/fGOrSUBUAAAAAElFTkSuQmCC";
const piece = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAIAAADYYG7QAAAAiElEQVR4nM3ZIRKEQAwF0aaLs+DWz/0No0HBadYi8PRzcakkKn/Zj4uSFRi/jYZ53hIjMeuzmOf9SRPjcTO5CUmMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMxEiMlP/UI5AxSIwEVzY/ShRe/QGDuwvapLmq0wAAAABJRU5ErkJggg==";

// Deterministic UI fixture only. The live server generates its own image and
// validates against a private database record; no target is returned by the API.
export function captchaFixture() {
  let sequence = 0;
  let challenge = "";
  let proof = "";
  const state = { issued: 0, verified: 0, rejected: 0, expiresIn: 120, failNext: false };
  return {
    state,
    async handle(route: Route) {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/cloud/auth/captcha/challenge") {
        challenge = `fixture-challenge-${++sequence}`;
        proof = "";
        state.issued++;
        await route.fulfill({ json: {
          challenge_id: challenge,
          background_image: background, piece_image: piece,
          image_width: 320, image_height: 160, piece_width: 48, piece_height: 48, piece_y: 48, expires_in: state.expiresIn,
        } });
        return true;
      }
      if (path === "/api/cloud/auth/captcha/verify") {
        const body = route.request().postDataJSON();
        const valid = body.challenge_id === challenge && challenge !== "" && Math.abs(body.x - 144) <= 4 && !state.failNext;
        challenge = "";
        state.failNext = false;
        if (!valid) {
          state.rejected++;
          await route.fulfill({ status: 403, json: { detail: { code: "captcha_failed", message: "验证未通过，请重试" } } });
        } else {
          state.verified++;
          proof = `fixture-proof-${sequence}`;
          await route.fulfill({ json: { proof, expires_in: state.expiresIn } });
        }
        return true;
      }
      return false;
    },
    consume(value: unknown) {
      const valid = typeof value === "string" && proof !== "" && value === proof;
      proof = "";
      return valid;
    },
  };
}

export async function completeCaptcha(page: Page) {
  const slider = page.getByRole("slider");
  await expect(slider).toBeEnabled();
  await slider.focus();
  await page.keyboard.press("Home");
  // Native range input uses a 1px step in original-image coordinates.
  for (let step = 0; step < 144; step++) await page.keyboard.press("ArrowRight");
  const verified = page.waitForResponse(response => new URL(response.url()).pathname === "/api/cloud/auth/captcha/verify");
  await page.keyboard.press("Enter");
  expect((await verified).status()).toBe(200);
}

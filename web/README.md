# Recon Agent — web dashboard

React 19 + TypeScript + Vite front end for the settlement reconciliation API.
See the [main README](../README.md#web-dashboard) for screenshots and the full tour.

## Run locally

```bash
# 1. start the API from the repo root (http://localhost:8000)
uvicorn src.api.app:app --reload

# 2. start the dashboard (http://localhost:5173)
cd web
npm install
npm run dev
```

## Configuration

| Variable       | Default                 | Purpose                         |
| -------------- | ----------------------- | ------------------------------- |
| `VITE_API_URL` | dev: `http://localhost:8000`, build: `/api` | Base URL of the FastAPI backend |

Copy `.env.example` to `.env.local` to override it.

## Scripts

- `npm run dev` — dev server with hot reload
- `npm run build` — type-check (`tsc -b`) and production build into `dist/`
- `npm run lint` — ESLint
- `npm run preview` — serve the production build

On Vercel the repo-root `vercel.json` builds this folder and sends every non-API route to
`index.html`, so deep links such as `/runs/<id>` work; `public/_redirects` does the same on Netlify.

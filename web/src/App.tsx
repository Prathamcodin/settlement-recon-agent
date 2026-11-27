import { createBrowserRouter, Link, RouterProvider } from "react-router-dom";
import Layout from "./Layout";
import GuardLab from "./pages/GuardLab";
import History from "./pages/History";
import Home from "./pages/Home";
import Results from "./pages/Results";
import Run from "./pages/Run";

function NotFound() {
  return (
    <div className="container center-page">
      <div className="stack gap-12" style={{ alignItems: "center" }}>
        <h1 style={{ fontSize: 26 }}>Page not found</h1>
        <Link to="/" className="btn btn-primary">
          Back to overview
        </Link>
      </div>
    </div>
  );
}

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: <Home /> },
      { path: "/run", element: <Run /> },
      { path: "/result", element: <Results /> },
      { path: "/runs", element: <History /> },
      { path: "/runs/:runId", element: <Results /> },
      { path: "/guard", element: <GuardLab /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

export default function App() {
  return <RouterProvider router={router} />;
}

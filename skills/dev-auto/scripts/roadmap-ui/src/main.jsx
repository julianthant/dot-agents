import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { readData } from "./shared.jsx";

const data = readData();
if (data) document.title = `${data.project} roadmap`;
createRoot(document.getElementById("root")).render(
  <StrictMode>{data ? <App data={data} /> : <p>No data in this page.</p>}</StrictMode>,
);

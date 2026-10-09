import fs from 'fs';
import path from 'path';

const baseDir = process.env.SUBMISSIONS_DIR || path.resolve(process.cwd(), '../react-instapost-submissions');

// 1x1 valid JPEG binary buffer
const jpegBase64 = '/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=';
const jpegBuffer = Buffer.from(jpegBase64, 'base64');

const packageJsonContent = {
  name: "react-instapost-project",
  private: true,
  version: "0.0.0",
  type: "module",
  scripts: {
    dev: "vite",
    build: "vite build",
    preview: "vite preview"
  },
  dependencies: {
    react: "^18.3.1",
    "react-dom": "^18.3.1"
  },
  devDependencies: {
    "@vitejs/plugin-react": "^4.3.1",
    vite: "^5.4.2"
  }
};

const viteConfigContent = `import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
});
`;

const indexHtmlContent = `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Mini Image Feed</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
`;

const mainJsxContent = `import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
`;

const indexCssContent = `body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  background-color: #fafafa;
  color: #262626;
}
`;

const appCssContent = `.posts {
  max-width: 450px;
  margin: 40px auto;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.posts h1 {
  text-align: center;
  font-size: 24px;
  margin-bottom: 16px;
}

.post-card {
  background: white;
  border: 1px solid #dbdbdb;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}

.post-image {
  width: 100%;
  height: 300px;
  object-fit: cover;
  display: block;
}

.post-author {
  font-weight: 600;
  padding: 12px 16px 4px;
  margin: 0;
}

.like-btn {
  margin: 8px 16px 16px;
  background-color: #0095f6;
  color: white;
  border: none;
  padding: 8px 16px;
  border-radius: 4px;
  cursor: pointer;
  font-weight: 600;
}

.like-btn:hover {
  background-color: #1877f2;
}
`;

function ensureDir(dir) {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function writeProjectBase(projectDir) {
  ensureDir(projectDir);
  ensureDir(path.join(projectDir, 'src'));
  ensureDir(path.join(projectDir, 'src', 'components'));
  ensureDir(path.join(projectDir, 'src', 'images'));

  fs.writeFileSync(path.join(projectDir, 'package.json'), JSON.stringify(packageJsonContent, null, 2));
  fs.writeFileSync(path.join(projectDir, 'vite.config.js'), viteConfigContent);
  fs.writeFileSync(path.join(projectDir, 'index.html'), indexHtmlContent);
  fs.writeFileSync(path.join(projectDir, 'src', 'main.jsx'), mainJsxContent);
  fs.writeFileSync(path.join(projectDir, 'src', 'index.css'), indexCssContent);
  fs.writeFileSync(path.join(projectDir, 'src', 'App.css'), appCssContent);

  fs.writeFileSync(path.join(projectDir, 'src', 'images', 'image1.jpg'), jpegBuffer);
  fs.writeFileSync(path.join(projectDir, 'src', 'images', 'image2.jpg'), jpegBuffer);
  fs.writeFileSync(path.join(projectDir, 'src', 'images', 'image3.jpg'), jpegBuffer);

  fs.writeFileSync(path.join(projectDir, '.gitignore'), `node_modules\ndist\n.env\n`);
}

// --------------------------------------------------------------------------------
// 1. SUBMISSION 1: 100% PERFECT
// --------------------------------------------------------------------------------
const dir100 = path.join(baseDir, 'submission-1-perfect-100');
writeProjectBase(dir100);

const app100 = `import React from 'react';
import Post from './components/post.jsx';
import image1 from './images/image1.jpg';
import image2 from './images/image2.jpg';
import image3 from './images/image3.jpg';
import './App.css';

function App() {
  return (
    <div className="posts">
      <h1>Mini Image Feed</h1>
      <Post image={image1} name="Gayathri" />
      <Post image={image2} name="Rahul" />
      <Post image={image3} name="Sneha" />
    </div>
  );
}

export default App;
`;

const post100 = `import React, { useState } from 'react';

const Post = (props) => {
  const [likes, setLikes] = useState(0);

  const handleLikes = () => {
    setLikes(likes + 1);
  };

  return (
    <div className="post-card">
      <img src={props.image} alt={props.name} className="post-image" />
      <p className="post-author">{props.name}</p>
      <button className="like-btn" onClick={handleLikes}>
        likes {likes}
      </button>
    </div>
  );
};

export default Post;
`;

fs.writeFileSync(path.join(dir100, 'src', 'App.jsx'), app100);
fs.writeFileSync(path.join(dir100, 'src', 'components', 'post.jsx'), post100);

// --------------------------------------------------------------------------------
// 2. SUBMISSION 2: 75% PARTIAL (Good component structure & props, buggy state update in handleLikes)
// --------------------------------------------------------------------------------
const dir75 = path.join(baseDir, 'submission-2-partial-75');
writeProjectBase(dir75);

const app75 = `import React from 'react';
import Post from './components/post.jsx';
import image1 from './images/image1.jpg';
import image2 from './images/image2.jpg';
import image3 from './images/image3.jpg';
import './App.css';

function App() {
  return (
    <div className="posts">
      <h1>Mini Image Feed</h1>
      <Post image={image1} name="Gayathri" />
      <Post image={image2} name="Rahul" />
      <Post image={image3} name="Sneha" />
    </div>
  );
}

export default App;
`;

// Student declared useState, but handleLikes fails to call setLikes or mutates directly
const post75 = `import React, { useState } from 'react';

const Post = (props) => {
  const [likes, setLikes] = useState(0);

  const handleLikes = () => {
    // Missing proper setLikes call to increment count
    console.log('Like clicked for:', props.name);
    // Student attempted direct mutation instead of setLikes(likes + 1)
    likes + 1;
  };

  return (
    <div className="post-card">
      <img src={props.image} alt={props.name} className="post-image" />
      <p className="post-author">{props.name}</p>
      <button className="like-btn" onClick={handleLikes}>
        likes {likes}
      </button>
    </div>
  );
};

export default Post;
`;

fs.writeFileSync(path.join(dir75, 'src', 'App.jsx'), app75);
fs.writeFileSync(path.join(dir75, 'src', 'components', 'post.jsx'), post75);

// --------------------------------------------------------------------------------
// 3. SUBMISSION 3: 50% HALF (Architecture & Props present, but State & Handler completely missing)
// --------------------------------------------------------------------------------
const dir50 = path.join(baseDir, 'submission-3-static-50');
writeProjectBase(dir50);

const app50 = `import React from 'react';
import Post from './components/post.jsx';
import image1 from './images/image1.jpg';
import image2 from './images/image2.jpg';
import image3 from './images/image3.jpg';
import './App.css';

function App() {
  return (
    <div className="posts">
      <h1>Mini Image Feed</h1>
      <Post image={image1} name="Gayathri" />
      <Post image={image2} name="Rahul" />
      <Post image={image3} name="Sneha" />
    </div>
  );
}

export default App;
`;

// Student has no useState, no likes/setLikes, no handleLikes
const post50 = `import React from 'react';

const Post = (props) => {
  return (
    <div className="post-card">
      <img src={props.image} alt={props.name} className="post-image" />
      <p className="post-author">{props.name}</p>
      <button className="like-btn">
        likes 0
      </button>
    </div>
  );
};

export default Post;
`;

fs.writeFileSync(path.join(dir50, 'src', 'App.jsx'), app50);
fs.writeFileSync(path.join(dir50, 'src', 'components', 'post.jsx'), post50);

// --------------------------------------------------------------------------------
// 4. SUBMISSION 4: 20% SKELETON (Monolith, no post.jsx, no props, bare template)
// --------------------------------------------------------------------------------
const dir20 = path.join(baseDir, 'submission-4-skeleton-20');
writeProjectBase(dir20);

// Student did not create post.jsx component, hardcoded basic static markup in App
const app20 = `import React from 'react';
import './App.css';

function App() {
  return (
    <div className="posts">
      <h1>Mini Image Feed</h1>
      <p>Feed loading...</p>
    </div>
  );
}

export default App;
`;

// No component or empty component
fs.writeFileSync(path.join(dir20, 'src', 'App.jsx'), app20);
// post.jsx is not created / missing

// Also create a master RUBRIC file for the assignment
const rubricJson = {
  title: "React Mini InstaPost Feed",
  description: "Students must create a mini Instagram-style feed with an App.jsx parent component and a reusable Post.jsx child component in src/components. App passes image and name props to three Post components. Each Post component manages its own independent 'likes' state with useState(0), renders an <img> with props.image, a <p> with props.name, and a <button> with handleLikes that increments likes by 1 on click.",
  criteria: [
    {
      name: "Component Architecture & Hierarchy",
      weight: 25,
      description: "App.jsx serves as the parent component rendering a container with className='posts' and heading 'Mini Image Feed'. A separate reusable Post child component is created in src/components/post.jsx and rendered 3 times in App."
    },
    {
      name: "Props Passing & Consumption",
      weight: 25,
      description: "App.jsx imports images and passes 'image' and 'name' props to each Post component. Post.jsx receives props and binds props.image to the <img> tag and props.name to the <p> tag."
    },
    {
      name: "Independent State Management",
      weight: 25,
      description: "Post.jsx uses useState to manage likes independently for each post. The state variable must be named 'likes' with setter 'setLikes', initialized to 0."
    },
    {
      name: "Event Handling & UI Interactivity",
      weight: 25,
      description: "Post.jsx defines a 'handleLikes' function that increments the like count using setLikes. The button triggers handleLikes onClick and displays the updated like count (e.g. 'likes {likes}')."
    }
  ]
};

fs.writeFileSync(path.join(baseDir, 'rubric.json'), JSON.stringify(rubricJson, null, 2));

console.log('All 4 test submissions and rubric.json generated successfully in:', baseDir);

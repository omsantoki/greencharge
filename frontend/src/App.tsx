import { Navigate, Route, Routes } from 'react-router-dom';
import OperatorDashboard from './pages/OperatorDashboard';

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<OperatorDashboard />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

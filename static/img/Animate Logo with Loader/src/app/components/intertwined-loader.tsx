import React, { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';

const IntertwinedLoader = () => {
  const [phase, setPhase] = useState<'logo' | 'intertwine' | 'loader'>('logo');

  useEffect(() => {
    const sequence = async () => {
      while (true) {
        setPhase('logo');
        await new Promise(r => setTimeout(r, 3000));
        setPhase('intertwine');
        await new Promise(r => setTimeout(r, 1500));
        setPhase('loader');
        await new Promise(r => setTimeout(r, 6000));
      }
    };
    sequence();
  }, []);

  // Heart Path from Logo
  const heartPath = "M11.2279 25.0885C12.7592 25.9867 14.6341 25.9867 16.1654 25.0885C19.3364 23.2269 20.3933 21.181 20.3933 19.4268C20.3933 17.546 18.6865 15.9607 16.6808 15.9607C14.6752 15.9607 13.6967 17.3694 13.6967 17.3694C13.6967 17.3694 12.7219 15.9607 10.7125 15.9607C8.70311 15.9607 7 17.5498 7 19.4268C7.00373 21.1772 8.05698 23.2269 11.2279 25.0885Z";
  
  // Speech Bubble/Logo part from Logo
  const logoPartPath = "M33.6226 14.6455V22.2148C33.6224 25.8015 31.2783 27.3417 27.8999 27.3418C26.544 27.4267 25.1962 27.0775 24.0522 26.3447V22.624C24.5878 23.0329 25.2467 23.2483 25.9204 23.2344C26.2069 23.2688 26.4976 23.2388 26.771 23.1465C27.0444 23.0541 27.2936 22.9014 27.5005 22.7002C27.7073 22.4991 27.8663 22.254 27.9663 21.9834C28.0662 21.7128 28.105 21.423 28.0786 21.1357V18.3672H24.0522V14.6455H33.6226Z";

  return (
    <div className="relative flex items-center justify-center w-48 h-48 overflow-hidden">
      <div className="relative z-10 w-full h-full flex items-center justify-center">
        <AnimatePresence mode="wait">
          {phase === 'logo' || phase === 'intertwine' ? (
            <motion.div
              key="logo-phase"
              className="w-full h-full"
              exit={{ opacity: 0, scale: 0.5, rotate: 180 }}
              transition={{ duration: 0.8, ease: "anticipate" }}
            >
              <svg viewBox="-15 -15 72 72" className="w-full h-full overflow-visible">
                <motion.path 
                  d={heartPath} 
                  fill="#E03E52"
                  initial={{ opacity: 0, scale: 0.5, rotate: -90, x: -10 }}
                  animate={{
                    opacity: 1,
                    x: phase === 'intertwine' ? [0, 12, -15, 8, 0] : 0,
                    y: phase === 'intertwine' ? [0, -10, 12, -8, 0] : 0,
                    scale: phase === 'intertwine' ? [1, 1.25, 0.9, 1.15, 1] : 1,
                    rotate: phase === 'intertwine' ? [0, 180, 360, 540, 720] : 0
                  }}
                  transition={{ 
                    duration: phase === 'intertwine' ? 1.5 : 0.8, 
                    ease: "easeInOut",
                    times: [0, 0.25, 0.5, 0.75, 1]
                  }}
                />
                <motion.path 
                  d={logoPartPath} 
                  fill="#1D1B1F"
                  initial={{ opacity: 0, scale: 0.5, rotate: 90, x: 10 }}
                  animate={{
                    opacity: phase === 'intertwine' ? 0.6 : 1,
                    x: phase === 'intertwine' ? [0, -18, 20, -10, 0] : 0,
                    y: phase === 'intertwine' ? [0, 12, -18, 12, 0] : 0,
                    scale: phase === 'intertwine' ? [1, 0.75, 1.1, 0.85, 1] : 1,
                    rotate: phase === 'intertwine' ? [0, -180, -360, -540, -720] : 0
                  }}
                  transition={{ 
                    duration: phase === 'intertwine' ? 1.5 : 0.8, 
                    ease: "easeInOut",
                    times: [0, 0.25, 0.5, 0.75, 1]
                  }}
                />
              </svg>
            </motion.div>
          ) : (
            <motion.div
              key="loader-phase"
              className="relative w-full h-full flex items-center justify-center"
              initial={{ opacity: 0, scale: 0, rotate: -180 }}
              animate={{ opacity: 1, scale: 1, rotate: 0 }}
              transition={{ type: "spring", damping: 12, stiffness: 100 }}
            >
              <motion.div 
                className="relative"
                animate={{
                  y: [-15, 15, -15],
                  rotate: [0, 360, 720]
                }}
                transition={{
                  duration: 2.88,
                  ease: [0.75, 0, 0.5, 1],
                  repeat: Infinity
                }}
              >
                <div className="relative flex items-center justify-center scale-[2]">
                  <motion.div 
                    className="absolute w-[1em] h-[1em] bg-[#E03E52] rounded-full"
                    style={{ x: '-0.45em', y: '-0.45em' }}
                    animate={{ scale: [1, 0.4, 1] }}
                    transition={{ duration: 2.88, repeat: Infinity, ease: [0.75, 0, 0.5, 1], delay: 2.88 * 0.2 }}
                  />
                  <motion.div 
                    className="absolute w-[1em] h-[1em] bg-[#E03E52] rounded-full"
                    style={{ x: '0.45em', y: '-0.45em' }}
                    animate={{ scale: [1, 0.4, 1] }}
                    transition={{ duration: 2.88, repeat: Infinity, ease: [0.75, 0, 0.5, 1], delay: 2.88 * 0.1 }}
                  />
                  <motion.div 
                    className="w-[1em] h-[1em] bg-[#E03E52]"
                    style={{ rotate: -45 }}
                    animate={{ 
                      borderRadius: ["0%", "50%", "0%"],
                      scale: [1, 0.5, 1]
                    }}
                    transition={{ duration: 2.88, repeat: Infinity, ease: [0.75, 0, 0.5, 1] }}
                  />
                </div>
              </motion.div>
              
              <motion.div 
                className="absolute bottom-4 w-16 h-2 bg-gray-300/40 rounded-full blur-sm"
                animate={{ 
                  scale: [1.2, 0.6, 1.2],
                  opacity: [0.6, 0.2, 0.6]
                }}
                transition={{ duration: 2.88, repeat: Infinity, ease: [0.75, 0, 0.5, 1] }}
              />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
};

export default IntertwinedLoader;
